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

import concurrent
import json
import logging
import os
import random
import shutil
import signal
import tempfile
import traceback
from abc import abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing import Process, Queue
from pathlib import Path
from queue import Empty
from time import sleep, time
from typing import Any, Dict, List, Optional, Tuple, TypeVar, Union
from urllib import parse

import boto3
import botocore
import numpy as np
import torch

from litgpt.litdata.constants import (
    _DEFAULT_FAST_DEV_RUN_ITEMS,
    _ENABLE_STATUS,
    _INDEX_FILENAME,
    _IS_IN_STUDIO,
    _TQDM_AVAILABLE,
)
from litgpt.litdata.processing.readers import BaseReader, StreamingDataLoaderReader
from litgpt.litdata.processing.utilities import _create_dataset, download_directory_from_S3, remove_uuid_from_filename
from litgpt.litdata.streaming import Cache
from litgpt.litdata.streaming.cache import Dir
from litgpt.litdata.streaming.client import S3Client
from litgpt.litdata.streaming.dataloader import StreamingDataLoader
from litgpt.litdata.streaming.resolver import _resolve_dir
from litgpt.litdata.utilities._pytree import tree_flatten, tree_unflatten, treespec_loads
from litgpt.litdata.utilities.broadcast import broadcast_object
from litgpt.litdata.utilities.dataset_utilities import load_index_file
from litgpt.litdata.utilities.encryption import Encryption
from litgpt.litdata.utilities.packing import _pack_greedily

logger = logging.Logger(__name__)


def _get_num_nodes() -> int:
    """Returns the number of nodes."""
    return int(os.getenv("DATA_OPTIMIZER_NUM_NODES", 1))


def _get_node_rank() -> int:
    """Returns the current node rank of the instance."""
    return int(os.getenv("DATA_OPTIMIZER_NODE_RANK", 0))


def _get_fast_dev_run() -> int:
    """Returns whether fast dev mode is enabled."""
    return bool(int(os.getenv("DATA_OPTIMIZER_FAST_DEV_RUN", 1)))


def _get_default_cache() -> str:
    return "/cache" if _IS_IN_STUDIO else tempfile.gettempdir()


def _get_cache_dir(name: Optional[str] = None) -> str:
    """Returns the cache directory used by the Cache to store the chunks."""
    cache_dir = os.getenv("DATA_OPTIMIZER_CACHE_FOLDER", f"{_get_default_cache()}/chunks")
    if name is None:
        return cache_dir
    return os.path.join(cache_dir, name.lstrip("/"))


def _get_cache_data_dir(name: Optional[str] = None) -> str:
    """Returns the cache data directory used by the DataProcessor workers to download the files."""
    cache_dir = os.getenv("DATA_OPTIMIZER_DATA_CACHE_FOLDER", f"{_get_default_cache()}/data")
    if name is None:
        return os.path.join(cache_dir)
    return os.path.join(cache_dir, name.lstrip("/"))


def _wait_for_file_to_exist(s3: S3Client, obj: parse.ParseResult, sleep_time: int = 2) -> Any:
    """This function check."""
    while True:
        try:
            return s3.client.head_object(Bucket=obj.netloc, Key=obj.path.lstrip("/"))
        except botocore.exceptions.ClientError as e:
            if "the HeadObject operation: Not Found" in str(e):
                sleep(sleep_time)
            else:
                raise e


def _wait_for_disk_usage_higher_than_threshold(input_dir: str, threshold_in_gb: int = 25, sleep_time: int = 3) -> None:
    usage = shutil.disk_usage(input_dir)

    while (usage.free / 1000 / 1000 / 1000) <= threshold_in_gb:
        sleep(sleep_time)
        usage = shutil.disk_usage(input_dir)

    return


def _download_data_target(input_dir: Dir, cache_dir: str, queue_in: Queue, queue_out: Queue) -> None:
    """This function is used to download data from a remote directory to a cache directory to optimise reading."""
    s3 = S3Client()

    while True:
        # 2. Fetch from the queue
        r: Optional[Tuple[int, List[str]]] = queue_in.get()

        # 3. Terminate the process if we received a termination signal
        if r is None:
            queue_out.put(None)
            return

        # 4. Unpack
        index, paths = r

        # 5. Check whether all the files are already downloaded
        if input_dir.path and all(
            os.path.exists(p.replace(input_dir.path, cache_dir) if input_dir else p) for p in paths
        ):
            queue_out.put(index)
            continue

        if input_dir.url is not None or input_dir.path is not None:
            if input_dir.url:
                # 6. Wait for the removers to catch up when we are downloading data.
                _wait_for_disk_usage_higher_than_threshold("/", 25)

            # 7. Download all the required paths to unblock the current index
            for path in paths:
                if input_dir.path:
                    local_path = path.replace(input_dir.path, cache_dir)

                if input_dir.url and input_dir.path:
                    path = path.replace(input_dir.path, input_dir.url)

                obj = parse.urlparse(path)

                if obj.scheme == "s3":
                    dirpath = os.path.dirname(local_path)

                    os.makedirs(dirpath, exist_ok=True)

                    with open(local_path, "wb") as f:
                        s3.client.download_fileobj(obj.netloc, obj.path.lstrip("/"), f)

                elif os.path.isfile(path):
                    if not path.startswith("/teamspace/studios/this_studio"):
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        # Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, local_path)
                else:
                    raise ValueError(f"The provided {input_dir.url} isn't supported.")

        # 7. Inform the worker the current files are available
        queue_out.put(index)


def _remove_target(input_dir: Dir, cache_dir: str, queue_in: Queue) -> None:
    """This function is used to delete files from the cache directory to minimise disk space."""
    while True:
        # 1. Collect paths
        paths = queue_in.get()

        # 2. Terminate the process if we received a termination signal
        if paths is None:
            return

        # 3. Iterate through the paths and delete them sequentially.
        for path in paths:
            if input_dir:
                if not path.startswith(cache_dir) and input_dir.path is not None:
                    path = path.replace(input_dir.path, cache_dir)

                if os.path.exists(path):
                    os.remove(path)

            elif os.path.exists(path) and "s3_connections" not in path:
                os.remove(path)


def _upload_fn(upload_queue: Queue, remove_queue: Queue, cache_dir: str, output_dir: Dir) -> None:
    """This function is used to upload optimised chunks from a local to remote dataset directory."""
    obj = parse.urlparse(output_dir.url if output_dir.url else output_dir.path)

    if obj.scheme == "s3":
        s3 = S3Client()

    while True:
        data: Optional[Union[str, Tuple[str, str]]] = upload_queue.get()

        tmpdir = None

        if isinstance(data, str) or data is None:
            local_filepath = data
        else:
            tmpdir, local_filepath = data

        # Terminate the process if we received a termination signal
        if local_filepath is None:
            return

        # Upload the file to the target cloud storage
        if not local_filepath.startswith(cache_dir):
            local_filepath = os.path.join(cache_dir, local_filepath)

        if obj.scheme == "s3":
            try:
                output_filepath = str(obj.path).lstrip("/")

                if local_filepath.__contains__(".checkpoints"):
                    output_filepath = os.path.join(output_filepath, ".checkpoints")
                if tmpdir is None:
                    output_filepath = os.path.join(output_filepath, os.path.basename(local_filepath))
                else:
                    output_filepath = os.path.join(output_filepath, local_filepath.replace(tmpdir, "")[1:])

                output_filepath = remove_uuid_from_filename(output_filepath)  # remove unique id from checkpoints

                s3.client.upload_file(
                    local_filepath,
                    obj.netloc,
                    output_filepath,
                )
            except Exception as e:
                print(e)

        elif output_dir.path:
            output_filepath = output_dir.path

            if local_filepath.__contains__(".checkpoints"):
                output_filepath = os.path.join(output_filepath, ".checkpoints")

            if tmpdir is None:
                output_filepath = os.path.join(output_filepath, os.path.basename(local_filepath))
            else:
                output_filepath = os.path.join(output_filepath, local_filepath.replace(tmpdir, "")[1:])

            output_filepath = remove_uuid_from_filename(output_filepath)  # remove unique id from checkpoints

            os.makedirs(os.path.dirname(output_filepath), exist_ok=True)
            shutil.copy(local_filepath, output_filepath)
        else:
            raise ValueError(f"The provided {output_dir.path} isn't supported.")

        # Inform the remover to delete the file
        if remove_queue and os.path.exists(local_filepath):
            remove_queue.put([local_filepath])


def _map_items_to_workers_sequentially(num_workers: int, user_items: List[Any]) -> List[List[Any]]:
    num_nodes = _get_num_nodes()
    world_size = num_nodes * num_workers
    num_items_per_worker = len(user_items) // world_size

    num_items_per_worker: List[int] = [num_items_per_worker for _ in range(world_size)]
    reminder = len(user_items) % world_size

    for worker_idx in range(len(num_items_per_worker) - 1, -1, -1):
        if reminder == 0:
            break
        num_items_per_worker[worker_idx] += 1
        reminder -= 1

    num_items_cumsum_per_worker = np.cumsum([0] + num_items_per_worker)

    out = []
    node_rank = _get_node_rank()
    worker_idx_start = node_rank * num_workers
    worker_idx_end = (node_rank + 1) * num_workers

    for worker_idx in range(world_size):
        if worker_idx_start <= worker_idx and worker_idx < worker_idx_end:
            start = num_items_cumsum_per_worker[worker_idx]
            end = num_items_cumsum_per_worker[worker_idx + 1]
            out.append(user_items[start:end])

    if len(out) != num_workers:
        raise RuntimeError("The items didn't haven't been assigned properly. Please, open an issue on Github.")

    return out


def _map_items_to_workers_weighted(
    num_workers: int,
    user_items: List[Any],
    weights: Optional[List[int]] = None,
    file_size: bool = True,
) -> List[List[Any]]:
    # Associate the items to the workers based on number of nodes and node rank.
    weights = [1] * len(user_items) if weights is None else weights
    num_nodes = _get_num_nodes()
    node_rank = _get_node_rank()
    world_size = num_nodes * num_workers

    worker_items, worker_weights = _pack_greedily(items=user_items, weights=weights, num_bins=world_size)
    worker_ids_this_node = range(node_rank * num_workers, (node_rank + 1) * num_workers)

    for worker_id, size in worker_weights.items():
        if worker_id not in worker_ids_this_node:
            continue

        if file_size:
            print(f"Worker {worker_id} gets {size / 1e6:.1f} MB ({len(worker_items[worker_id])} files)")
        else:
            print(f"Worker {worker_id} gets ({len(worker_items[worker_id])}) items for a total weight of {size}.")

    return [np.random.permutation(worker_items[worker_id]).tolist() for worker_id in worker_ids_this_node]


def _get_num_bytes(item: Any, base_path: str) -> int:
    flattened_item, _ = tree_flatten(item)

    num_bytes = 0
    for element in flattened_item:
        if isinstance(element, str):
            element = Path(element).resolve()
            if not element.exists():
                continue
            file_bytes = os.path.getsize(element)
            if file_bytes == 0:
                raise RuntimeError(f"The file {element} has 0 bytes!")
            num_bytes += file_bytes
    return num_bytes


def _get_item_filesizes(items: List[Any], base_path: str = "") -> List[int]:
    """Computes the total size in bytes of all file paths for every datastructure in the given list."""
    item_sizes = []

    cpu_count = os.cpu_count() or 1

    # Parallelize to accelerate retrieving the number of file bytes to read for each item
    with concurrent.futures.ThreadPoolExecutor(max_workers=cpu_count * 2 if cpu_count > 4 else cpu_count) as executor:
        futures = [executor.submit(_get_num_bytes, item, base_path) for item in items]
        for future in futures:
            item_sizes.append(future.result())
    return item_sizes


def _to_path(element: str) -> str:
    return element if _IS_IN_STUDIO and element.startswith("/teamspace") else str(Path(element).resolve())


def _is_path(input_dir: Optional[str], element: Any) -> bool:
    if not isinstance(element, str):
        return False

    if _IS_IN_STUDIO and input_dir is not None:
        if element.startswith(input_dir):
            return True

        element = str(Path(element).absolute())
        if element.startswith(input_dir):
            # check whether the element has an extension.
            if os.path.splitext(element)[1]:
                return True
            return os.path.isfile(element)

    return os.path.isfile(element)


class BaseWorker:
    def __init__(
        self,
        worker_index: int,
        num_workers: int,
        node_rank: int,
        data_recipe: "DataRecipe",
        input_dir: Dir,
        output_dir: Dir,
        items: List[Any],
        progress_queue: Queue,
        error_queue: Queue,
        stop_queue: Queue,
        num_downloaders: int,
        num_uploaders: int,
        remove: bool,
        reader: Optional[BaseReader] = None,
        writer_starting_chunk_index: int = 0,
        use_checkpoint: bool = False,
        checkpoint_chunks_info: Optional[List[Dict[str, Any]]] = None,
        checkpoint_next_index: Optional[int] = None,
    ) -> None:
        """The BaseWorker is responsible to process the user data."""
        self.worker_index = worker_index
        self.num_workers = num_workers
        self.node_rank = node_rank
        self.data_recipe = data_recipe
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.items = items
        self.num_items = len(self.items)
        self.num_downloaders = num_downloaders
        self.num_uploaders = num_uploaders
        self.remove = remove
        self.reader = reader
        self.paths: List[List[str]] = []
        self.remover: Optional[Process] = None
        self.downloaders: List[Process] = []
        self.uploaders: List[Process] = []
        self.to_download_queues: List[Queue] = []
        self.to_upload_queues: List[Queue] = []
        self.stop_queue = stop_queue
        self.ready_to_process_queue: Queue = Queue()
        self.remove_queue: Queue = Queue()
        self.progress_queue: Queue = progress_queue
        self.error_queue: Queue = error_queue
        self._counter = 0
        self._last_time = time()
        self._index_counter = 0
        self.writer_starting_chunk_index: int = writer_starting_chunk_index
        self.use_checkpoint: bool = use_checkpoint
        self.checkpoint_chunks_info: Optional[List[Dict[str, Any]]] = checkpoint_chunks_info
        self.checkpoint_next_index: Optional[int] = checkpoint_next_index

    def run(self) -> None:
        try:
            self._setup()
            self._loop()
        except Exception:
            traceback_format = traceback.format_exc()
            self.error_queue.put(traceback_format)
        print(f"Worker {str(_get_node_rank() * self.num_workers + self.worker_index)} is done.")

    def _setup(self) -> None:
        self._set_environ_variables()
        self._create_cache()
        self._collect_paths()
        self._start_downloaders()
        self._start_uploaders()
        self._start_remover()

    def _loop(self) -> None:
        num_downloader_finished = 0

        while True:
            index = self.ready_to_process_queue.get()

            if index is None:
                num_downloader_finished += 1
                if num_downloader_finished == self.num_downloaders:
                    print(f"Worker {str(_get_node_rank() * self.num_workers + self.worker_index)} is terminating.")

                    if isinstance(self.data_recipe, DataChunkRecipe):
                        self._handle_data_chunk_recipe_end()

                    if self.output_dir.url if self.output_dir.url else self.output_dir.path:
                        # Inform the uploaders they are doing working
                        for i in range(self.num_uploaders):
                            self.to_upload_queues[i].put(None)

                        # Wait for them all to be finished
                        for uploader in self.uploaders:
                            uploader.join()

                    if self.remove:
                        assert self.remover
                        self.remove_queue.put(None)
                        self.remover.join()

                    if self.progress_queue:
                        self.progress_queue.put((self.worker_index, self._counter))
                    return
                continue

            if isinstance(self.data_recipe, DataChunkRecipe):
                self._handle_data_chunk_recipe(index)
            else:
                self._handle_data_transform_recipe(index)

            self._counter += 1

            # Don't send the last progress update, so the main thread awaits for the uploader and remover
            if self.progress_queue and (time() - self._last_time) > 1 and self._counter < (self.num_items - 2):
                self.progress_queue.put((self.worker_index, self._counter))
                self._last_time = time()

            if self.remove and self.input_dir.path is not None and self.reader is None:
                self.remove_queue.put(self.paths[index])

            try:
                self.stop_queue.get(timeout=0.0001)
                return
            except Empty:
                pass

    def _set_environ_variables(self) -> None:
        # set the optimizer global rank and world_size
        os.environ["DATA_OPTIMIZER_GLOBAL_RANK"] = str(_get_node_rank() * self.num_workers + self.worker_index)
        os.environ["DATA_OPTIMIZER_NUM_WORKERS"] = str(self.num_workers)

    def _create_cache(self) -> None:
        self.cache_data_dir = _get_cache_data_dir()
        os.makedirs(self.cache_data_dir, exist_ok=True)

        self.cache_chunks_dir = _get_cache_dir()

        if os.path.exists(self.cache_chunks_dir):
            # clean up the cache chunks dir folder to avoid previous json files from interfering with the current run
            shutil.rmtree(self.cache_chunks_dir, ignore_errors=True)

        os.makedirs(self.cache_chunks_dir, exist_ok=True)

        if isinstance(self.data_recipe, DataTransformRecipe):
            return
        self.cache = Cache(
            self.cache_chunks_dir,
            chunk_bytes=self.data_recipe.chunk_bytes,
            chunk_size=self.data_recipe.chunk_size,
            compression=self.data_recipe.compression,
            encryption=self.data_recipe.encryption,
            writer_chunk_index=self.writer_starting_chunk_index,
        )
        self.cache._reader._rank = _get_node_rank() * self.num_workers + self.worker_index

        # return
        if self.use_checkpoint and all(
            [
                self.checkpoint_chunks_info is not None,
                self.checkpoint_next_index is not None,
            ]
        ):
            assert isinstance(self.checkpoint_next_index, int)
            assert isinstance(self.checkpoint_chunks_info, list)

            self.cache._writer._chunks_info = self.checkpoint_chunks_info
            self.cache._writer._chunk_index += self.checkpoint_next_index

    def _try_upload(self, data: Optional[Union[str, Tuple[str, str]]]) -> None:
        if not data or (self.output_dir.url if self.output_dir.url else self.output_dir.path) is None:
            return

        if isinstance(data, str):
            assert os.path.exists(data), data
        else:
            assert os.path.exists(data[-1]), data

        self.to_upload_queues[self._counter % self.num_uploaders].put(data)

    def _collect_paths(self) -> None:
        if self.input_dir.path is None or self.reader is not None:
            for index in range(len(self.items)):
                self.ready_to_process_queue.put(index)
            for _ in range(self.num_downloaders):
                self.ready_to_process_queue.put(None)
            return

        items = []
        for item in self.items:
            flattened_item, spec = tree_flatten(item)

            # For speed reasons, we assume starting with `self.input_dir` is enough to be a real file.
            # Other alternative would be too slow.
            # TODO: Try using dictionary for higher accurary.
            indexed_paths = {
                index: _to_path(element)
                for index, element in enumerate(flattened_item)
                if _is_path(self.input_dir.path, element)
            }

            if len(indexed_paths) == 0:
                raise ValueError(
                    f"The provided item {item} didn't contain any filepaths. The input_dir is {self.input_dir.path}."
                )

            paths = []
            for index, path in indexed_paths.items():
                paths.append(path)
                if self.input_dir and not self.input_dir.path.startswith("/teamspace/studios/this_studio"):
                    path = path.replace(self.input_dir.path, self.cache_data_dir)
                flattened_item[index] = path

            self.paths.append(paths)

            items.append(tree_unflatten(flattened_item, spec))

        self.items = items

    def _start_downloaders(self) -> None:
        if self.input_dir.path is None or self.reader is not None:
            return

        for _ in range(self.num_downloaders):
            to_download_queue: Queue = Queue()
            p = Process(
                target=_download_data_target,
                args=(
                    self.input_dir,
                    self.cache_data_dir,
                    to_download_queue,
                    self.ready_to_process_queue,
                ),
            )
            p.start()
            self.downloaders.append(p)
            self.to_download_queues.append(to_download_queue)

        for index, paths in enumerate(self.paths):
            self.to_download_queues[index % self.num_downloaders].put((index, paths))

        for downloader_index in range(self.num_downloaders):
            self.to_download_queues[downloader_index].put(None)

    def _start_remover(self) -> None:
        if not self.remove:
            return

        self.remover = Process(
            target=_remove_target,
            args=(
                self.input_dir,
                self.cache_data_dir,
                self.remove_queue,
            ),
        )
        self.remover.start()

    def _start_uploaders(self) -> None:
        if self.output_dir.path is None and self.output_dir.url is None:
            return

        for _ in range(self.num_uploaders):
            to_upload_queue: Queue = Queue()
            p = Process(
                target=_upload_fn,
                args=(
                    to_upload_queue,
                    self.remove_queue,
                    self.cache_chunks_dir,
                    self.output_dir,
                ),
            )
            p.start()
            self.uploaders.append(p)
            self.to_upload_queues.append(to_upload_queue)

    def _handle_data_chunk_recipe(self, index: int) -> None:
        try:
            current_item = self.items[index] if self.reader is None else self.reader.read(self.items[index])
            item_data_or_generator = self.data_recipe.prepare_item(current_item)
            if self.data_recipe.is_generator:
                for item_data in item_data_or_generator:
                    if item_data is not None:
                        chunk_filepath = self.cache._add_item(self._index_counter, item_data)
                        self._try_upload(chunk_filepath)
                        self._index_counter += 1
            elif item_data_or_generator is not None:
                chunk_filepath = self.cache._add_item(self._index_counter, item_data_or_generator)
                self._try_upload(chunk_filepath)
                self._index_counter += 1
                if self.use_checkpoint:
                    checkpoint_filepath = self.cache.save_checkpoint()
                    self._try_upload(checkpoint_filepath)
        except Exception as e:
            raise RuntimeError(f"Failed processing {self.items[index]=}; {index=}") from e

    def _handle_data_chunk_recipe_end(self) -> None:
        chunks_filepaths = self.cache.done()

        if chunks_filepaths and len(self.to_upload_queues):
            for i, chunk_filepath in enumerate(chunks_filepaths):
                if isinstance(chunk_filepath, str) and os.path.exists(chunk_filepath):
                    self.to_upload_queues[i % self.num_uploaders].put(chunk_filepath)

        if self.use_checkpoint and not self.data_recipe.is_generator:
            checkpoint_filepath = self.cache.save_checkpoint()
            self._try_upload(checkpoint_filepath)

    def _handle_data_transform_recipe(self, index: int) -> None:
        # Don't use a context manager to avoid deleting files that are being uploaded.
        output_dir = tempfile.mkdtemp()
        item = self.items[index] if self.reader is None else self.reader.read(self.items[index])
        item_data = self.data_recipe.prepare_item(item, str(output_dir), len(self.items) - 1 == index)
        if item_data is not None:
            raise ValueError(
                "When using a `DataTransformRecipe`, the `prepare_item` shouldn't return anything."
                " Simply store your files under the output_dir."
            )
        filepaths = []
        for directory, _, filenames in os.walk(output_dir):
            for filename in filenames:
                filepaths.append(os.path.join(directory, filename))

        for filepath in filepaths:
            self._try_upload((output_dir, filepath))


class DataWorkerProcess(BaseWorker, Process):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """The DataWorkerProcess is responsible to process the user data inside processes."""
        BaseWorker.__init__(self, *args, **kwargs)
        Process.__init__(self)


@dataclass
class _Result:
    size: Optional[int] = None
    num_bytes: Optional[str] = None
    data_format: Optional[str] = None
    compression: Optional[str] = None
    encryption: Optional[Encryption] = None
    num_chunks: Optional[int] = None
    num_bytes_per_chunk: Optional[List[int]] = None


T = TypeVar("T")


class DataRecipe:
    @abstractmethod
    def prepare_structure(self, input_dir: Optional[str]) -> List[T]:
        pass

    @abstractmethod
    def prepare_item(self, *args: Any, **kwargs: Any) -> Any:
        pass

    def __init__(self) -> None:
        self._name: Optional[str] = None

    def _done(self, size: int, delete_cached_files: bool, output_dir: Dir) -> _Result:
        return _Result(size=size)


class DataChunkRecipe(DataRecipe):
    def __init__(
        self,
        chunk_size: Optional[int] = None,
        chunk_bytes: Optional[Union[int, str]] = None,
        compression: Optional[str] = None,
        encryption: Optional[Encryption] = None,
    ):
        super().__init__()
        if chunk_size is not None and chunk_bytes is not None:
            raise ValueError("Either one of the `chunk_size` or the `chunk_bytes` need to be provided.")

        self.chunk_size = chunk_size
        self.chunk_bytes = 1 << 26 if chunk_size is None and chunk_bytes is None else chunk_bytes
        self.compression = compression
        self.encryption = encryption

    @abstractmethod
    def prepare_structure(self, input_dir: Optional[str]) -> List[T]:
        """Return the structure of your data.

        Each element should contain at least a filepath.

        """

    @abstractmethod
    def prepare_item(self, item_metadata: T) -> Any:
        """The return of this `prepare_item` method is persisted in chunked binary files."""

    def _done(self, size: int, delete_cached_files: bool, output_dir: Dir) -> _Result:
        num_nodes = _get_num_nodes()
        cache_dir = _get_cache_dir()

        chunks = [file for file in os.listdir(cache_dir) if file.endswith(".bin")]
        if chunks and delete_cached_files and output_dir.path is not None:
            raise RuntimeError(f"All the chunks should have been deleted. Found {chunks}")

        merge_cache = Cache(cache_dir, chunk_bytes=1)
        node_rank = _get_node_rank()
        merge_cache._merge_no_wait(node_rank if num_nodes > 1 else None, getattr(self, "existing_index", None))

        self._upload_index(output_dir, cache_dir, num_nodes, node_rank)

        if num_nodes == node_rank + 1:
            config = load_index_file(cache_dir)

            size = sum([c["dim"] if c["dim"] is not None else c["chunk_size"] for c in config["chunks"]])
            num_bytes = sum([c["chunk_bytes"] for c in config["chunks"]])
            if config["config"] is not None:
                data_format = tree_unflatten(
                    config["config"]["data_format"], treespec_loads(config["config"]["data_spec"])
                )
            else:
                data_format = None
            num_chunks = len(config["chunks"])

            # The platform can't store more than 1024 entries.
            # Note: This isn't really used right now, so it is fine to skip if too big.
            num_bytes_per_chunk = [c["chunk_size"] for c in config["chunks"]] if num_chunks < 1024 else []
            return _Result(
                size=size,
                num_bytes=num_bytes,
                data_format=data_format,
                compression=config["config"]["compression"] if config["config"] else None,
                num_chunks=len(config["chunks"]),
                num_bytes_per_chunk=num_bytes_per_chunk,
            )
        return _Result(
            size=size,
        )

    def _upload_index(self, output_dir: Dir, cache_dir: str, num_nodes: int, node_rank: Optional[int]) -> None:
        """This method upload the index file to the remote cloud directory."""
        if output_dir.path is None and output_dir.url is None:
            return

        obj = parse.urlparse(output_dir.url if output_dir.url else output_dir.path)
        if num_nodes > 1:
            local_filepath = os.path.join(cache_dir, f"{node_rank}-{_INDEX_FILENAME}")
        else:
            local_filepath = os.path.join(cache_dir, _INDEX_FILENAME)

        if obj.scheme == "s3":
            s3 = S3Client()
            s3.client.upload_file(
                local_filepath, obj.netloc, os.path.join(str(obj.path).lstrip("/"), os.path.basename(local_filepath))
            )
        elif output_dir.path and os.path.isdir(output_dir.path):
            shutil.copyfile(local_filepath, os.path.join(output_dir.path, os.path.basename(local_filepath)))

        if num_nodes == 1 or node_rank is None:
            return

        # Merge the index files generated by each node.
        # Note: When using the Data Optimizer, they should be a single process on each node executing this section
        # So no risk to get race conditon.
        if num_nodes == node_rank + 1:
            # Get the index file locally
            for node_rank in range(num_nodes - 1):
                output_dir_path = output_dir.url if output_dir.url else output_dir.path
                assert output_dir_path
                remote_filepath = os.path.join(output_dir_path, f"{node_rank}-{_INDEX_FILENAME}")
                node_index_filepath = os.path.join(cache_dir, os.path.basename(remote_filepath))
                if obj.scheme == "s3":
                    obj = parse.urlparse(remote_filepath)
                    _wait_for_file_to_exist(s3, obj)
                    with open(node_index_filepath, "wb") as f:
                        s3.client.download_fileobj(obj.netloc, obj.path.lstrip("/"), f)
                elif output_dir.path and os.path.isdir(output_dir.path):
                    shutil.copyfile(remote_filepath, node_index_filepath)

            merge_cache = Cache(cache_dir, chunk_bytes=1)
            merge_cache._merge_no_wait()
            self._upload_index(output_dir, cache_dir, 1, None)


class DataTransformRecipe(DataRecipe):
    @abstractmethod
    def prepare_structure(self, input_dir: Optional[str]) -> List[T]:
        """Return the structure of your data.

        Each element should contain at least a filepath.

        """

    @abstractmethod
    def prepare_item(self, item_metadata: T, output_dir: str, is_last: bool) -> None:
        """Use your item metadata to process your files and save the file outputs into `output_dir`."""


class DataProcessor:
    def __init__(
        self,
        input_dir: Union[str, Dir],
        output_dir: Optional[Union[str, Dir]] = None,
        num_workers: Optional[int] = None,
        num_downloaders: Optional[int] = None,
        num_uploaders: Optional[int] = None,
        delete_cached_files: bool = True,
        fast_dev_run: Optional[Union[bool, int]] = None,
        random_seed: Optional[int] = 42,
        reorder_files: bool = True,
        weights: Optional[List[int]] = None,
        reader: Optional[BaseReader] = None,
        state_dict: Optional[Dict[int, int]] = None,
        use_checkpoint: bool = False,
    ):
        """The `DatasetOptimiser` provides an efficient way to process data across multiple machine into chunks to make
        training faster.

        Arguments:
            input_dir: The path to where the input data are stored.
            output_dir: The path to where the output data are stored.
            num_workers: The number of worker threads to use.
            num_downloaders: The number of file downloaders to use.
            num_uploaders: The number of file uploaders to use.
            delete_cached_files: Whether to delete the cached files.
            fast_dev_run: Whether to run a quick dev run.
            random_seed: The random seed to be set before shuffling the data.
            reorder_files: By default, reorders the files by file size to distribute work equally among all workers.
                Set this to ``False`` if the order in which samples are processed should be preserved.
            weights: Provide a list of weights associated to the inputs.
                This is used to evenly split the work among the workers.
            reader: Map the inputs to worker inputs and provides a read method to read a slice of the data.
            state_dict: The writer state dict. This is used to decide how to append data to an existing dataset.
            use_checkpoint: Whether to create checkpoints while processing the data, which can be used to resume the
                processing from the last checkpoint if the process is interrupted. (`Default: False`)

        """
        self.input_dir = _resolve_dir(input_dir)
        self.output_dir = _resolve_dir(output_dir)

        self.num_workers = num_workers or (1 if fast_dev_run else (os.cpu_count() or 1) * 4)
        self.num_downloaders = num_downloaders or 2
        self.num_uploaders = num_uploaders or 5
        self.delete_cached_files = delete_cached_files
        self.fast_dev_run = _get_fast_dev_run() if fast_dev_run is None else fast_dev_run
        self.workers: Any = []
        self.workers_tracker: Dict[int, int] = {}
        self.progress_queue: Optional[Queue] = None
        self.error_queue: Queue = Queue()
        self.stop_queues: List[Queue] = []
        self.reorder_files = reorder_files
        self.weights = weights
        self.reader = reader
        self.use_checkpoint = use_checkpoint
        self.checkpoint_chunks_info: Optional[List[List[Dict[str, Any]]]] = None
        self.checkpoint_next_index: Optional[List[int]] = None

        self.state_dict = state_dict or {rank: 0 for rank in range(self.num_workers)}

        if self.reader is not None and self.weights is not None:
            raise ValueError("Either the reader or the weights needs to be defined.")

        # Ensure the input dir is the same across all nodes
        self.input_dir = broadcast_object("input_dir", self.input_dir)

        if self.output_dir:
            # Ensure the output dir is the same across all nodes
            self.output_dir = broadcast_object("output_dir", self.output_dir, rank=_get_node_rank())
            print(f"Storing the files under {self.output_dir.path if self.output_dir.path else self.output_dir.url}")

        self.random_seed = random_seed

    def run(self, data_recipe: DataRecipe) -> None:
        """The `DataProcessor.run(...)` method triggers the data recipe processing over your dataset."""
        if not isinstance(data_recipe, DataRecipe):
            raise ValueError("The provided value should be a data recipe.")
        if not self.use_checkpoint and isinstance(data_recipe, DataChunkRecipe):
            # clean up checkpoints if not using checkpoints
            self._cleanup_checkpoints()

        t0 = time()
        print(f"Setup started with fast_dev_run={self.fast_dev_run}.")

        # Force random seed to be fixed
        random.seed(self.random_seed)
        np.random.seed(self.random_seed)
        torch.manual_seed(self.random_seed)

        # Call the setup method of the user
        user_items: List[Any] = data_recipe.prepare_structure(self.input_dir.path if self.input_dir else None)
        if not isinstance(user_items, (list, StreamingDataLoader)):
            raise ValueError("The `prepare_structure` should return a list of item metadata.")

        if isinstance(user_items, StreamingDataLoader):
            self.reader = StreamingDataLoaderReader(user_items)

        if self.reader:
            user_items = self.reader.remap_items(user_items, self.num_workers)

        if self.weights is not None:
            if len(self.weights) != len(user_items):
                raise ValueError("The provided weights length should match the inputs' length.")
            workers_user_items = _map_items_to_workers_weighted(
                num_workers=self.num_workers, user_items=user_items, weights=self.weights, file_size=False
            )

        elif self.reorder_files and self.input_dir.path:
            # TODO: Only do this on node 0, and broadcast the item sizes to the other nodes.
            item_sizes = _get_item_filesizes(user_items, base_path=self.input_dir.path)
            workers_user_items = _map_items_to_workers_weighted(
                num_workers=self.num_workers, user_items=user_items, weights=item_sizes
            )
        else:
            workers_user_items = _map_items_to_workers_sequentially(num_workers=self.num_workers, user_items=user_items)

        print(f"Setup finished in {round(time() - t0, 3)} seconds. Found {len(user_items)} items to process.")

        if self.use_checkpoint:
            if hasattr(data_recipe, "is_generator") and data_recipe.is_generator:
                # Checkpoint feature is not supported for generators for now.
                raise ValueError("Checkpoint feature is not supported for generators, yet.")
            # get the last checkpoint details
            print("Resuming from last saved checkpoint...")
            self._load_checkpoint_config(workers_user_items)

            assert isinstance(self.checkpoint_next_index, list)

            if all(self.checkpoint_next_index[i] == 0 for i in range(self.num_workers)):
                # save the current configuration in the checkpoints.json file
                print("No checkpoints found. Saving current configuration...")
                self._save_current_config(workers_user_items)
            else:
                # load the last checkpoint details
                assert isinstance(self.checkpoint_next_index, list)
                workers_user_items = [w[self.checkpoint_next_index[i] :] for i, w in enumerate(workers_user_items)]
                print("Checkpoints loaded successfully.")

        if self.fast_dev_run:
            items_to_keep = self.fast_dev_run if isinstance(self.fast_dev_run, int) else _DEFAULT_FAST_DEV_RUN_ITEMS
            workers_user_items = [w[:items_to_keep] for w in workers_user_items]
            print(f"Fast dev run is enabled. Limiting to {items_to_keep} items per process.")

        num_items = sum([len(items) for items in workers_user_items])

        self._cleanup_cache()

        print(
            f"Starting {self.num_workers} workers with {num_items} items."
            f" The progress bar is only updated when a worker finishes."
        )

        if self.input_dir is None and self.src_resolver is not None and self.input_dir:
            self.input_dir = self.src_resolver(self.input_dir)
            print(f"The remote_dir is `{self.input_dir}`.")

        signal.signal(signal.SIGINT, self._signal_handler)

        self._create_process_workers(data_recipe, workers_user_items)

        print("Workers are ready ! Starting data processing...")

        current_total = 0
        if _TQDM_AVAILABLE:
            from tqdm.auto import tqdm as _tqdm

            pbar = _tqdm(
                desc="Progress",
                total=num_items,
                smoothing=0,
                position=-1,
                mininterval=1,
                leave=True,
                dynamic_ncols=True,
            )
        num_nodes = _get_num_nodes()
        node_rank = _get_node_rank()
        total_num_items = len(user_items)

        while True:
            try:
                error = self.error_queue.get(timeout=0.001)
                self._exit_on_error(error)
            except Empty:
                assert self.progress_queue
                try:
                    index, counter = self.progress_queue.get(timeout=0.001)
                except Empty:
                    continue
                self.workers_tracker[index] = counter
                new_total = sum(self.workers_tracker.values())

            if _TQDM_AVAILABLE:
                pbar.update(new_total - current_total)

            current_total = new_total
            if current_total == num_items:
                break

            if _IS_IN_STUDIO and node_rank == 0 and _ENABLE_STATUS:
                with open("status.json", "w") as f:
                    json.dump({"progress": str(100 * current_total * num_nodes / total_num_items) + "%"}, f)

            # Exit early if all the workers are done.
            # This means there were some kinda of errors.
            if all(not w.is_alive() for w in self.workers):
                raise RuntimeError("One of the worker has failed")

        if _TQDM_AVAILABLE:
            pbar.close()

        # TODO: Understand why it hangs.
        if num_nodes == 1:
            for w in self.workers:
                w.join(0)

        print("Workers are finished.")
        result = data_recipe._done(len(user_items), self.delete_cached_files, self.output_dir)

        if num_nodes == node_rank + 1 and self.output_dir.url and self.output_dir.path is not None and _IS_IN_STUDIO:
            from lightning_cloud.openapi import V1DatasetType

            _create_dataset(
                input_dir=self.input_dir.path,
                storage_dir=self.output_dir.path,
                dataset_type=V1DatasetType.CHUNKED
                if isinstance(data_recipe, DataChunkRecipe)
                else V1DatasetType.TRANSFORMED,
                empty=False,
                size=result.size,
                num_bytes=result.num_bytes,
                data_format=result.data_format,
                compression=result.compression,
                num_chunks=result.num_chunks,
                num_bytes_per_chunk=result.num_bytes_per_chunk,
            )

        print("Finished data processing!")
        if self.use_checkpoint and isinstance(data_recipe, DataChunkRecipe):
            # clean up checkpoints
            self._cleanup_checkpoints()

    def _exit_on_error(self, error: str) -> None:
        for w in self.workers:
            # w.join(0)
            w.terminate()  # already error has occurred. So, no benefit of processing further.
        raise RuntimeError(f"We found the following error {error}.")

    def _create_process_workers(self, data_recipe: DataRecipe, workers_user_items: List[List[Any]]) -> None:
        self.progress_queue = Queue()
        workers: List[DataWorkerProcess] = []
        stop_queues: List[Queue] = []
        for worker_idx, worker_user_items in enumerate(workers_user_items):
            stop_queues.append(Queue())
            worker = DataWorkerProcess(
                worker_idx,
                self.num_workers,
                _get_node_rank(),
                data_recipe,
                self.input_dir,
                self.output_dir,
                worker_user_items,
                self.progress_queue,
                self.error_queue,
                stop_queues[-1],
                self.num_downloaders,
                self.num_uploaders,
                self.delete_cached_files,
                self.reader,
                self.state_dict[worker_idx],
                self.use_checkpoint,
                self.checkpoint_chunks_info[worker_idx] if self.checkpoint_chunks_info else None,
                self.checkpoint_next_index[worker_idx] if self.checkpoint_next_index else None,
            )
            worker.start()
            workers.append(worker)

        # Note: Don't store within the loop as weakref aren't serializable
        self.workers = workers
        self.stop_queues = stop_queues

    def _signal_handler(self, signal: Any, frame: Any) -> None:
        """On temrination, we stop all the processes to avoid leaking RAM."""
        for stop_queue in self.stop_queues:
            stop_queue.put(None)
        for w in self.workers:
            w.join(0)
        os._exit(0)

    def _cleanup_cache(self) -> None:
        cache_dir = _get_cache_dir()

        # Cleanup the cache dir folder to avoid corrupted files from previous run to be there.
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)

        os.makedirs(cache_dir, exist_ok=True)

        cache_data_dir = _get_cache_data_dir()

        # Cleanup the cache data folder to avoid corrupted files from previous run to be there.
        if os.path.exists(cache_data_dir):
            shutil.rmtree(cache_data_dir, ignore_errors=True)

        os.makedirs(cache_data_dir, exist_ok=True)

    def _cleanup_checkpoints(self) -> None:
        if not isinstance(self.output_dir, Dir):
            raise ValueError("The provided output_dir isn't a Dir Object.")

        if self.output_dir.url is None:
            # this is a local directory
            if self.output_dir.path is None:
                return

            if os.path.exists(self.output_dir.path):
                # clear the checkpoints
                with suppress(FileNotFoundError):
                    shutil.rmtree(os.path.join(self.output_dir.path, ".checkpoints"))

            return

        obj = parse.urlparse(self.output_dir.url)

        if obj.scheme != "s3":
            raise ValueError(f"The provided folder should start with s3://. Found {self.output_dir.path}.")

        s3 = boto3.client("s3")

        prefix = obj.path.lstrip("/").rstrip("/") + "/"

        # Delete all the files (including the index file in overwrite mode)
        bucket_name = obj.netloc
        s3 = boto3.resource("s3")

        checkpoint_prefix = os.path.join(prefix, ".checkpoints")

        for obj in s3.Bucket(bucket_name).objects.filter(Prefix=checkpoint_prefix):
            s3.Object(bucket_name, obj.key).delete()

    def _save_current_config(self, workers_user_items: List[List[Any]]) -> None:
        if not self.use_checkpoint:
            return

        # save the current configuration in the config.json file
        config = {
            "num_workers": self.num_workers,
            "workers_user_items": workers_user_items,
        }

        try:
            if self.output_dir.url is None:
                assert self.output_dir.path

                if not os.path.exists(os.path.join(self.output_dir.path, ".checkpoints")):
                    os.makedirs(os.path.join(self.output_dir.path, ".checkpoints"))

                with open(os.path.join(self.output_dir.path, ".checkpoints", "config.json"), "w") as f:
                    json.dump(config, f)

                return

            obj = parse.urlparse(self.output_dir.url)

            if obj.scheme != "s3":
                raise ValueError(f"The provided folder should start with s3://. Found {self.output_dir.path}.")

            # TODO: Add support for all cloud providers

            s3 = S3Client()

            prefix = obj.path.lstrip("/").rstrip("/") + "/" + ".checkpoints/"

            # write config.json file to temp directory and upload it to s3
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_file_name = os.path.join(temp_dir, "config.json")
                with open(temp_file_name, "w") as f:
                    json.dump(config, f)
                s3.client.upload_file(
                    temp_file_name,
                    obj.netloc,
                    os.path.join(prefix, "config.json"),
                )
        except Exception as e:
            print(e)

    def _load_checkpoint_config(self, workers_user_items: List[List[Any]]) -> None:
        if not self.use_checkpoint:
            return

        default_chunk_info: List[Dict[str, Any]] = []

        self.checkpoint_chunks_info = [default_chunk_info for _ in range(self.num_workers)]
        self.checkpoint_next_index = [0 for _ in range(self.num_workers)]

        if self.output_dir.url is None:
            assert self.output_dir.path

            if not os.path.exists(os.path.join(self.output_dir.path, ".checkpoints")):
                return

            if not os.path.exists(os.path.join(self.output_dir.path, ".checkpoints", "config.json")):
                # if the config.json file doesn't exist, we don't have any checkpoint saved
                return

            with open(os.path.join(self.output_dir.path, ".checkpoints", "config.json")) as f:
                config = json.load(f)

            if config["num_workers"] != self.num_workers:
                raise ValueError(
                    "The number of workers in the checkpoints doesn't match the current number of workers."
                )

            if config["workers_user_items"] != workers_user_items:
                raise ValueError("Existing checkpoints are not compatible with the current configuration.")

            checkpoint_file_names = [f"checkpoint-{worker_idx}.json" for worker_idx in range(self.num_workers)]

            for i, checkpoint_file_name in enumerate(checkpoint_file_names):
                if not os.path.exists(os.path.join(self.output_dir.path, ".checkpoints", checkpoint_file_name)):
                    # if the checkpoint file doesn't exist, we don't have any checkpoint saved for this worker
                    continue

                with open(os.path.join(self.output_dir.path, ".checkpoints", checkpoint_file_name)) as f:
                    checkpoint = json.load(f)

                self.checkpoint_chunks_info[i] = checkpoint["chunks"]
                self.checkpoint_next_index[i] = checkpoint["done_till_index"]
            return

        obj = parse.urlparse(self.output_dir.url)

        if obj.scheme != "s3":
            raise ValueError(f"The provided folder should start with s3://. Found {self.output_dir.path}.")

        # TODO: Add support for all cloud providers

        prefix = obj.path.lstrip("/").rstrip("/") + "/" + ".checkpoints/"

        # Delete all the files (including the index file in overwrite mode)
        bucket_name = obj.netloc

        # download all the checkpoint files in tempdir and read them
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_file_dir = download_directory_from_S3(bucket_name, prefix, temp_dir)

            if not os.path.exists(os.path.join(saved_file_dir, "config.json")):
                # if the config.json file doesn't exist, we don't have any checkpoint saved
                return

            # read the config.json file
            with open(os.path.join(saved_file_dir, "config.json")) as f:
                config = json.load(f)

            if config["num_workers"] != self.num_workers:
                raise ValueError(
                    "The number of workers in the checkpoints doesn't match the current number of workers."
                )

            if config["workers_user_items"] != workers_user_items:
                raise ValueError("Existing checkpoints are not compatible with the current configuration.")

            checkpoint_file_names = [f"checkpoint-{worker_idx}.json" for worker_idx in range(self.num_workers)]

            for i, checkpoint_file_name in enumerate(checkpoint_file_names):
                if not os.path.exists(os.path.join(saved_file_dir, checkpoint_file_name)):
                    # if the checkpoint file doesn't exist, we don't have any checkpoint saved for this worker
                    continue

                with open(os.path.join(saved_file_dir, checkpoint_file_name)) as f:
                    checkpoint = json.load(f)

                self.checkpoint_chunks_info[i] = checkpoint["chunks"]
                self.checkpoint_next_index[i] = checkpoint["done_till_index"]
        return
