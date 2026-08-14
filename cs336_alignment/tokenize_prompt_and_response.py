from transformers import PreTrainedTokenizer
import torch
from torch.nn.functional import pad 

def tokenize_prompt_and_output(
    prompt_strs: list[str],
    output_strs: list[str],
    tokenizer: PreTrainedTokenizer,
) -> dict[str, torch.Tensor]:
    '''
    prompt_strs and output_strs have the same number of samples: batch_size,
    but their lengths are very different from each other.
    '''
    prompt_lens = []
    output_lens = []
    prompt_and_output_lens = []
    concats = []
    prompts_encoded = []
    outputs_encoded = []
    for prompt_str, output_str in zip(prompt_strs, output_strs):
        prompt_encoded = tokenizer.encode(prompt_str)
        output_encoded = tokenizer.encode(output_str)
        prompt_and_output_lens.append(
            len(prompt_encoded) + len(output_encoded)
        )
        prompt_lens.append(len(prompt_encoded))
        output_lens.append(len(output_encoded))
        prompts_encoded.append(prompt_encoded)
        outputs_encoded.append(output_encoded)
    max_prompt_and_output_lens = max(prompt_and_output_lens)
    
    for prompt_encoded, output_encoded in zip(prompts_encoded, outputs_encoded):
        concat = torch.concat([
                torch.tensor(prompt_encoded), 
                torch.tensor(output_encoded)
        ])
        concat_padded = pad(
            concat,
            (0,max_prompt_and_output_lens-len(prompt_encoded)-len(output_encoded)),
            mode='constant',
            value=0
        )
        concats.append(concat_padded)
    concats_encoded = torch.stack(concats)
    input_ids = concats_encoded[:, :-1]
    labels = concats_encoded[:, 1:]

    response_mask = torch.zeros_like(labels)
    for i, (prompt_len, output_len) in enumerate(zip(prompt_lens, output_lens)):
        response_mask[i, prompt_len-1:prompt_len-1+output_len] = 1

    return {
        'input_ids': input_ids,
        'labels': labels,
        'response_mask': response_mask
    }

    