import os, json


src_file = "data/raw/GSM/GSM8K_test.jsonl"
tgt_file = "data/cooked/GSM8K_test.jsonl"

tgt_dir = os.path.dirname(tgt_file)
os.makedirs(tgt_dir, exist_ok=True)

with open(src_file, "r") as src_f, open(tgt_file, "w") as tgt_f:
    cnt = 0
    for line in src_f:
        data = json.loads(line)
        
        id_ = f"GSM8K-{cnt:04d}"
        query = data["query"]
        response = data["response"]
        new_data = {
            "id": id_,
            "problem": query,
            "gt_solution": response,
            "gt_answer": None
        }
        new_data_str = json.dumps(new_data)
        tgt_f.write(new_data_str + "\n")
        cnt += 1
print(f"Finished writing {cnt} lines to {tgt_file}")