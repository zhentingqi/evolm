# Copyright The Lightning AI team.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
from typing import Any, Dict, List, Tuple

import numpy as np

from litgpt.litdata.streaming.item_loader import Interval
from litgpt.litdata.utilities.env import _DistributedEnv


def _intra_node_chunk_shuffle(
    distributed_env: _DistributedEnv,
    num_workers: int,
    chunks_per_workers: List[List[int]],
    seed: int,
    current_epoch: int,
) -> List[int]:
    chunk_indexes_per_nodes = _group_chunks_by_nodes(
        chunks_per_workers=chunks_per_workers,
        world_size=distributed_env.world_size,
        num_nodes=distributed_env.num_nodes,
        num_workers_per_process=num_workers,
    )

    # shuffle the chunks associated to the node
    for i in range(len(chunk_indexes_per_nodes)):
        # permute the indexes within the node
        chunk_indexes_per_nodes[i] = list(
            np.random.RandomState(seed=seed + current_epoch).permutation(chunk_indexes_per_nodes[i])
        )

    return [index for chunks in chunk_indexes_per_nodes for index in chunks]


def _group_chunks_by_nodes(
    chunks_per_workers: List[List[int]],
    world_size: int,
    num_nodes: int,
    num_workers_per_process: int,
) -> List[List[int]]:
    """Takes a list representing chunks grouped by worker (global worker id across ranks and nodes) and returns a list
    in which the chunks are grouped by node."""
    chunk_indexes_per_nodes: Any = [[] for _ in range(num_nodes)]
    num_processes_per_node = world_size // num_nodes
    for worker_global_id, chunks in enumerate(chunks_per_workers):
        process_rank = worker_global_id // num_workers_per_process  # the process rank this worker belongs to
        node_rank = process_rank // num_processes_per_node  # the node rank this worker belongs to
        chunk_indexes_per_nodes[node_rank].extend(chunks)
    return chunk_indexes_per_nodes


def _associate_chunks_and_intervals_to_workers(
    distributed_env: _DistributedEnv,
    indexes: Any,
    chunk_intervals: List[Interval],
    drop_last: bool = False,
    num_workers: int = 1,
    batch_size: int = 1,
) -> Tuple[List[List[int]], List[Any]]:
    num_items = sum([(interval[2] - interval[1]) for interval in chunk_intervals])
    world_size = distributed_env.world_size * num_workers
    num_items_per_workers: List[int] = [
        num_items // world_size + num_items % world_size
        if rank == world_size - 1 and not drop_last
        else num_items // world_size
        for rank in range(world_size)
    ]
    if drop_last:
        num_items_per_workers = [batch_size * int(item // batch_size) for item in num_items_per_workers]

    chunks_per_workers: List[List[int]] = [[] for _ in range(world_size)]
    intervals_per_workers: List[List[List[int]]] = [[] for _ in range(world_size)]

    # 4. Assign the chunk & intervals to each rank
    for chunk_index, chunk_interval in zip(indexes, chunk_intervals):
        rank = 0

        while True:
            if rank == len(num_items_per_workers):
                break

            items_left_to_assign = num_items_per_workers[rank]

            if items_left_to_assign == 0:
                rank += 1
                continue

            items_in_chunk = chunk_interval[2] - chunk_interval[1]

            if items_in_chunk == 0:
                break

            if items_in_chunk > items_left_to_assign:
                chunks_per_workers[rank].append(chunk_index)

                chunk_start, chunk_roi_start, chunk_roi_end, chunk_end = chunk_interval

                intervals_per_workers[rank].append(
                    [chunk_start, chunk_roi_start, chunk_roi_start + items_left_to_assign, chunk_end]
                )
                chunk_interval = Interval(chunk_start, chunk_roi_start + items_left_to_assign, chunk_roi_end, chunk_end)
                num_items_per_workers[rank] = 0
                rank += 1
            else:
                chunks_per_workers[rank].append(chunk_index)
                intervals_per_workers[rank].append(list(chunk_interval))
                num_items_per_workers[rank] -= items_in_chunk
                break

    return chunks_per_workers, intervals_per_workers


def _find_chunks_per_workers_on_which_to_skip_deletion(
    num_workers: int,
    batch_size: int,
    workers_chunks: List[List[int]],
    workers_intervals: List[List[Interval]],
) -> Dict[int, List[int]]:
    """Returns a dictionary mapping a chunk index to a list of workers that should not delete that chunk.

    If a worker is included in this list, it should not delete the chunk after fully reading it, because another worker
    will still have items left to read and therefore needs the chunk to be present. This mapping is used in the dataset
    to only let the worker delete a chunk when that worker is the last to read from it.

    """

    # Shared chunks across all workers and ranks
    shared_chunks = _get_shared_chunks(workers_chunks)

    # Shared chunks grouped together by rank
    shared_chunks_aggregated_by_rank = _aggregate_shared_chunks_per_rank(shared_chunks, num_workers)

    max_trackers = {}
    for chunk_index, map_local_rank_to_worker_ids in shared_chunks_aggregated_by_rank.items():
        for local_rank, workers_index_sharing_chunks_for_this_rank in map_local_rank_to_worker_ids.items():
            # Get all the worker chunks and intervals for this distributed rank
            workers_slice = slice(local_rank * num_workers, (local_rank + 1) * num_workers)
            workers_chunks_for_this_rank = copy.deepcopy(workers_chunks[workers_slice])
            workers_interval_sizes_for_this_rank = copy.deepcopy(
                [
                    [interval[2] - interval[1] for interval in worker_intervals]
                    for worker_intervals in workers_intervals[workers_slice]
                ]
            )

            num_shared_workers_for_this_rank = len(workers_index_sharing_chunks_for_this_rank)
            worker_tracker_idx = 0
            num_of_samples_to_carry_to_next_chunk = None
            counter = 0

            while True:
                interval_size_of_current_worker = workers_interval_sizes_for_this_rank[worker_tracker_idx % num_workers]
                if len(interval_size_of_current_worker) == 0:
                    worker_tracker_idx += 1
                    continue

                num_samples_left_for_this_worker_chunk = interval_size_of_current_worker[0]

                # To consume a batch, we want to subtract `batch_size` from the size we have left,
                # unless we had a remainder (< batch size) from the previous iteration/chunk
                remover = (
                    batch_size
                    if num_of_samples_to_carry_to_next_chunk is None
                    else num_of_samples_to_carry_to_next_chunk
                )

                if num_samples_left_for_this_worker_chunk > remover:
                    # There are samples left to consume, so we subtract the batch size (or a remainder)
                    workers_interval_sizes_for_this_rank[worker_tracker_idx % num_workers][0] -= remover
                    counter += remover
                    num_of_samples_to_carry_to_next_chunk = None
                else:
                    # There are fewer samples left in this chunk than we would like to consume for a full batch
                    # So we take what's left from the chunk and move to the next chunk to complete the batch
                    current_worker_chunk_index = workers_chunks_for_this_rank[worker_tracker_idx % num_workers].pop(0)
                    workers_interval_sizes_for_this_rank[worker_tracker_idx % num_workers].pop(0)
                    counter += remover

                    if current_worker_chunk_index == chunk_index:
                        num_shared_workers_for_this_rank -= 1

                    # TODO: Maybe, we can prevent loading over and over for each worker
                    if num_shared_workers_for_this_rank == 0 and current_worker_chunk_index == chunk_index:
                        # We consumed entirely the chunk of the worker we were tracking
                        # Keep track of how many samples this worker consumed for this chunk and which worker
                        # has consumed the most samples for this chunk
                        if chunk_index not in max_trackers:
                            max_trackers[chunk_index] = (
                                local_rank * num_workers + worker_tracker_idx % num_workers,
                                counter,
                            )
                        else:
                            if max_trackers[chunk_index][1] < counter:
                                max_trackers[chunk_index] = (
                                    local_rank * num_workers + worker_tracker_idx % num_workers,
                                    counter,
                                )
                        break

                    if num_samples_left_for_this_worker_chunk != batch_size:
                        # If a batch was not assembled completely because we're at the end of a chunk,
                        # we need to complete the assembly from samples in the next chunk and carry
                        # over that remainder to the next loop iteration
                        num_of_samples_to_carry_to_next_chunk = batch_size - num_samples_left_for_this_worker_chunk

                    if remover != batch_size:
                        # We've handled the remainder, reset it. Next iteration will start a fresh batch.
                        num_of_samples_to_carry_to_next_chunk = None

                if num_of_samples_to_carry_to_next_chunk is None:
                    # Only go to the next worker if we assembled a full batch. If we have a remainder,
                    # we need to go to the next chunk with the same worker and complete the batch.
                    worker_tracker_idx += 1

    to_disable = {}
    for chunk_index, worker_ids in shared_chunks.items():
        last_worker_idx = max_trackers[chunk_index][0]
        to_disable[chunk_index] = [worker_idx for worker_idx in worker_ids if worker_idx != last_worker_idx]
    return to_disable


def _get_shared_chunks(workers_chunks: List[List[int]]) -> Dict[int, List[int]]:
    """Returns a dictionary mapping a chunk index to a list of workers that share that same chunk."""
    shared_chunks = {}
    for worker, chunks in enumerate(workers_chunks):
        for chunk in chunks:
            if chunk not in shared_chunks:
                shared_chunks[chunk] = [worker]
            else:
                shared_chunks[chunk].append(worker)
    # Remove chunk indexes that are only read by a single worker (and thus not shared)
    return {chunk: workers for chunk, workers in shared_chunks.items() if len(workers) > 1}


def _aggregate_shared_chunks_per_rank(
    shared_chunks: Dict[int, List[int]], num_workers: int
) -> Dict[int, Dict[int, List[int]]]:
    """Groups together shared chunks by rank.

    The output is a dictionary mapping a chunk index to a dictionary that maps a rank to a list of workers.

    """
    aggregated_shared_chunks_per_rank: Dict[int, Dict[int, List[int]]] = {}
    for chunk_index, workers_ids in shared_chunks.items():
        aggregated_shared_chunks_per_rank[chunk_index] = {}
        for worker_idx in workers_ids:
            if (worker_idx // num_workers) not in aggregated_shared_chunks_per_rank[chunk_index]:
                aggregated_shared_chunks_per_rank[chunk_index][worker_idx // num_workers] = []
            aggregated_shared_chunks_per_rank[chunk_index][worker_idx // num_workers].append(worker_idx)
    return aggregated_shared_chunks_per_rank


def _map_node_worker_rank_to_chunk_indexes_to_not_delete(to_disable: Dict[int, List[int]]) -> Dict[int, List[int]]:
    """Takes a dictionary mapping a chunk index to a list of workers and inverts the map such that it returns a
    dictionary mapping a worker to a list of chunk indexes (that should not be deleted by that worker)."""
    map_node_worker_rank_to_chunk_indexes: Dict[int, List[int]] = {}
    for chunk_index, worker_ids in to_disable.items():
        for worker_idx in worker_ids:
            if worker_idx not in map_node_worker_rank_to_chunk_indexes:
                map_node_worker_rank_to_chunk_indexes[worker_idx] = []
            map_node_worker_rank_to_chunk_indexes[worker_idx].append(chunk_index)
    return map_node_worker_rank_to_chunk_indexes
