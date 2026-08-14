from cs336_alignment.vllm_utils import VLLMServer
from cs336_alignment.drgrpo_grader import question_only_reward_fn, r1_zero_reward_fn

from importlib.resources import files
import json
from tqdm import tqdm

GSM8K_PATH = '/global/cfs/cdirs/m4410/mzheng/cs336/assignment5-alignment/data/gsm8k/test.jsonl'


QUESTION_ONLY_PROMPT = (
    files("cs336_alignment")
    .joinpath("prompts/question_only.prompt")
    .read_text(encoding='utf-8')
)
R1_ZERO_PROMPT = (
    files("cs336_alignment")
    .joinpath("prompts/r1_zero.prompt")
    .read_text(encoding='utf-8')
)
R1_ZERO_FEW_SHOT_PROMPT = (
    files("cs336_alignment")
    .joinpath("prompts/r1_zero_three_shot_gsm8k.prompt")
    .read_text(encoding='utf-8')
)

sampling_params_question_only = {
    'temperature': 1.0,
    'max_tokens': 512,
    'stop': ["</answer>"],
    "top_p": 1.0,
    'n': 1,
}
sampling_params_r1_zero = {
    'temperature': 1.0,
    'max_tokens': 512,
    'stop': ["</answer>"],
    "top_p": 1.0,
    'n': 1,
    "include_stop_str_in_output": True,
}
# only use the following for non-question_only tasks
# "include_stop_str_in_output": True,

def load_json(json_file: str):
    data = []
    with open(json_file, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                data.append(json.loads(line))
    return data

def reformat(data, template, repeat=1):
    if repeat == 1:
        return [
            template.format(question=d['question'])
            for d in data
        ]
    ret = []
    for d in data:
        ret.extend([template.format(question=d['question'])] * repeat)
    return ret
    
def extract_gsm8k_ground_truth(data, repeat=1):
    if isinstance(data, dict):
        return data['answer'].split("####")[-1].strip()
    if repeat == 1:
        return [
            d['answer'].split("####")[-1].strip() for d in data
        ]
    ret = []
    for d in data:
        ret.extend([d['answer'].split("####")[-1].strip()] * repeat)
    return ret

def run(server, gsm8k_test, template, template_type, reward_fn):
    assert template_type in ['question_only', 'r1_zero', 'r1_zero_three_shot']
    gsm8k_test_formatted = reformat(gsm8k_test, template)

    try:
        server.start()
        completions = server.generate_completions(
            prompts=gsm8k_test_formatted,
            sampling_params=sampling_params_question_only if template_type == 'question_only' else sampling_params_r1_zero,
            batch_size=64,
        )

        results_lst = []
        total_answer_reward = 0
        total_template_reward = 0
        both_correct = 0
        correct_format_wrong_answer = 0
        both_incorrect = 0
        for i in tqdm(range(len(gsm8k_test_formatted)), desc=f'GSM8K {template_type}'):
            data = gsm8k_test[i]
            question = data['question']
            answer = data['answer']
            ground_truth = extract_gsm8k_ground_truth(data)
            response = completions[i].text
            res = reward_fn(response, ground_truth)
            result_dict = {
                'question': question,
                'answer': answer,
                'ground_truth': ground_truth,
                'response': response,
                'format_reward': res['format_reward'],
                'answer_reward': res['answer_reward']
            }
            reward = res['answer_reward']
            results_lst.append(result_dict)
            total_answer_reward += reward
            total_template_reward += res['format_reward']
            if res['format_reward'] and res['answer_reward']:
                both_correct += 1
            elif res['format_reward']:
                correct_format_wrong_answer += 1
            else:
                both_incorrect += 1
        print(f"[{template_type}] answer_reward / num_questions: {total_answer_reward}/{len(gsm8k_test)}")
        print(f"[{template_type}] template_reward / num_questions: {total_template_reward}/{len(gsm8k_test)}")
        print(f"[{template_type}] Both_correct: {both_correct}")
        print(f"[{template_type}] Correct_format_wrong_answer: {correct_format_wrong_answer}")
        print(f"[{template_type}] Both_incorrect: {both_incorrect}")

        log_path = f'/global/cfs/cdirs/m4410/mzheng/cs336/assignment5-alignment/cs336_alignment/prompting/gsm8k_{template_type}.txt'
        with open(log_path, 'w', encoding='utf-8') as f:
            json.dump(results_lst, f, indent=4)
        
    finally:
        server.stop()

if __name__ == '__main__':
    gsm8k_test = load_json(GSM8K_PATH)

    server = VLLMServer(
        model_id='allenai/OLMo-2-0425-1B',
        gpu=0,
        logging_level='INFO',
        host='localhost',
        seed=0,
    )
    run(
        server, gsm8k_test, 
        template=QUESTION_ONLY_PROMPT, 
        template_type='question_only',
        reward_fn=question_only_reward_fn
    )
    run(
        server, gsm8k_test, 
        template=R1_ZERO_PROMPT, 
        template_type='r1_zero',
        reward_fn=r1_zero_reward_fn,
    )
    run(
        server, gsm8k_test, 
        template=R1_ZERO_FEW_SHOT_PROMPT, 
        template_type='r1_zero_three_shot',
        reward_fn=r1_zero_reward_fn
    )
