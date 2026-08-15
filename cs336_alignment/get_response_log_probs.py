import torch.nn.functional as F
import torch
from transformers import PreTrainedModel

def get_response_log_probs(
    model: PreTrainedModel,
    input_ids: torch.Tensor,
    labels: torch.Tensor,
    return_token_entropy: bool = False,
) -> dict[str, torch.Tensor]:
    '''
    Args:
        model: PreTrainedModel HuggingFace model used for scoring (placed on the correct device and in inference mode if gradients should not be computed).
        
        input_ids: torch.Tensor shape (batch_size, sequence_length), concatenated prompt + response tokens as produced by your tokenization method.
        
        labels: torch.Tensor shape (batch_size, sequence_length), labels as produced by your tokenization method.
        
        return_token_entropy: bool If True, also return per-token entropy
    Returns:
        dict[str, torch.Tensor].
            "log_probs" shape (batch_size, sequence_length), conditional log-probabilities log𝑝𝜃(𝑥𝑡|𝑥<𝑡).
            
            "token_entropy" optional, shape (batch_size, sequence_length), per-token entropy for each position (present only if return_token_entropy=True)
    '''
    input_ids = input_ids.to(model.device)
    labels = labels.to(model.device)
    
    logits_res = model(input_ids).logits
    probs = F.softmax(logits_res, dim=-1)
    log_probs = F.log_softmax(logits_res, dim=-1)
    log_probs_labels = log_probs.gather(dim=-1, index=labels.unsqueeze(-1)).squeeze(-1)
    ret = {
        "log_probs": log_probs_labels
    }
    if return_token_entropy:
        ret['token_entropy'] = -(probs * log_probs).sum(dim=-1)
    return ret