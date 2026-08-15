from typing import Any, Callable, Literal
import torch
from torch import Tensor
from torch.utils.data import Dataset
from transformers import PreTrainedTokenizerBase
from transformers import PreTrainedModel
from transformers import PreTrainedTokenizer
from torch.optim import Optimizer, AdamW

from cs336_alignment.get_response_log_probs import get_response_log_probs
from cs336_alignment.tokenize_prompt_and_response import tokenize_prompt_and_output
from torch.nn.utils import clip_grad_norm_

from importlib.resources import files
import argparse
import os
import wandb
import json

from cs336_alignment.vllm_utils import VLLMServer
from cs336_alignment.prompting.main import reformat, extract_gsm8k_ground_truth

from cs336_alignment.checkpoint import get_model_and_tokenizer

from cs336_alignment.drgrpo_grader import r1_zero_reward_fn

R1_ZERO_PROMPT = (
    files("cs336_alignment")
    .joinpath("prompts/r1_zero.prompt")
    .read_text(encoding='utf-8')
)

def compute_rollout_rewards(
    reward_fn: Callable[[str, str], dict[str, float]],
    rollout_responses: list[str],
    repeated_ground_truths: list[str],
) -> tuple[torch.Tensor, dict[str, float]]:
    '''
    rollout_batch_size = n_prompts_per_rollout_batch * group_size

    Args:
        reward_fn: Callable[[str, str], dict[str, float]] Scores the rollout responses against the ground truths, producing a dict with keys "reward", "format_reward", and "answer_reward".

        rollout_responses: list[str] Rollouts from the policy. The length of this list is rollout_batch_size = n_prompts_per_rollout_batch * group_size.

        repeated_ground_truths: list[str] The ground truths for the examples. The length of this list is rollout_batch_size, because the ground truth for each example is repeated group_size times.

    Returns:
        tuple[torch.Tensor, dict[str, float]].

        raw_rewards shape (rollout_batch_size,). Unnormalized rewards for each rollout response.

        metadata Reward statistics to log. At minimum, include the mean total and format rewards over the rollout batch.
    '''
    raw_rewards = []
    metadata = {} # At minimum, include the mean total and format rewards over the rollout batch.
    total_reward = 0.0
    format_reward = 0.0
    n = len(rollout_responses)
    for rollout_response, repeated_ground_truth in zip(rollout_responses, repeated_ground_truths):
        reward = reward_fn(rollout_response, repeated_ground_truth)
        raw_rewards.append(reward['reward'])
        total_reward += reward['reward']
        format_reward += reward['format_reward']
    metadata['mean_total_reward'] = total_reward / n   
    metadata['mean_format_reward'] = format_reward / n
    return torch.tensor(raw_rewards), metadata

def compute_group_normalized_rewards(
    raw_rewards: torch.Tensor,
    group_size: int,
    baseline: Literal["mean", "none"] = "mean",
    advantage_eps: float = 1e-6,
    advantage_normalizer: Literal["std", "none", "mean"] = "std",
):
    '''
    Args:
        raw_rewards: torch.Tensor shape (rollout_batch_size,). Unnormalized rewards for each rollout response, where rollout_batch_size = n_prompts_per_rollout_batch * group_size.

        group_size: int Number of responses per question (group).

        baseline: Literal["mean", "none"] For this problem, support mean, which subtracts the per-group mean reward. Later, none will mean no baseline subtraction.

        advantage_eps: float Small constant to avoid division by zero in normalization.

        advantage_normalizer: Literal["std", "none", "mean"] For this problem, support std, which divides by the per-group standard deviation. Later, none will mean no normalization and mean will mean divide by the per-group mean reward.
    Returns:
        tuple[torch.Tensor, dict[str, float]].
        
        advantages shape (rollout_batch_size,). Group-normalized rewards for each rollout response.
        
        metadata your choice of other statistics to log (e.g.mean, std, max/min of rewards).
    '''
    rollout_batch_size = raw_rewards.shape[0]
    raw_rewards = raw_rewards.reshape(-1, group_size)

    if baseline == 'mean':
        baseline_vals = raw_rewards.mean(dim=-1, keepdim=True)
    else:
        baseline_vals = torch.zeros(group_size)
    advantages = raw_rewards - baseline_vals
    
    if advantage_normalizer == 'std':
        advantage_normalizer_val = torch.std(raw_rewards, dim=-1, keepdim=True)
    else:
        raise NotImplementedError
    
    # metadata record the mean, std, max, min of the per-group reward
    metadata = {
        "mean": raw_rewards.mean(dim=-1),
        "std": advantage_normalizer_val,
        "max": raw_rewards.max(dim=-1),
        "min": raw_rewards.min(dim=-1)
    }
    
    group_normalized_reward = (advantages / (advantage_eps + advantage_normalizer_val))
    group_normalized_reward = group_normalized_reward.reshape(rollout_batch_size,)
    return (
        group_normalized_reward, 
        metadata
    )

def compute_policy_gradient_loss(
    raw_rewards_or_advantages: torch.Tensor,
    policy_log_probs: torch.Tensor,
    importance_reweighting_method: Literal["none", "noclip", "grpo", "gspo"] = "none",
    old_log_probs: torch.Tensor | None = None,
    cliprange: float | None = None,
    response_mask: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    '''
    Args:
        raw_rewards_or_advantages: torch.Tensor Shape (batch_size,) or (batch_size, 1), scalar reward/advantage for each rollout response.

        policy_log_probs: torch.Tensor Shape (batch_size, sequence_length), logprobs for each token.

        importance_reweighting_method: Literal["none", "noclip", "grpo", "gspo"] "none": no importance reweighting; "noclip": apply importance reweighting without clipping; "grpo": do PPO/GRPO-style token-level reweighting and clipping; "gspo": do GSPO-style sequence-level reweighting and clipping.

        old_log_probs: torch.Tensor | None Required unless importance_reweighting_method = "none"; shape (batch_size, sequence_length).

        cliprange: float | None = None Clip parameter 𝜀, required when importance_reweighting_method is "grpo" or "gspo".

        response_mask: torch.Tensor | None = None Optional shape (batch_size, sequence_length) mask over response tokens. Required for GSPO implementations that average the sequence-level log-ratio over response tokens only.
    
    Returns:
        tuple[torch.Tensor, dict[str, torch.Tensor]].

        per_token_policy_gradient_loss Shape (batch_size, sequence_length), the per-token policy-gradient loss (to be aggregated across the batch and sequence dimensions in the training loop).

        metadata Statistics from the underlying loss call, such as clip-fraction components.
    '''
    if importance_reweighting_method != 'none':
        raise NotImplementedError
    return -raw_rewards_or_advantages.view(-1,1) * policy_log_probs, None

def aggregate_loss_across_microbatch(
    per_token_policy_gradient_loss: torch.Tensor,
    mask: torch.Tensor,
    loss_normalization: Literal["sequence", "constant"] = "sequence",
    normalization_constant: int | None = None,
) -> torch.Tensor:
    '''
    Args:
        per_token_policy_gradient_loss: torch.Tensor Shape (batch_size, sequence_length), the per-token policy-gradient loss (to be aggregated across the batch and sequence dimensions in the training loop).

        mask torch.Tensor of shape (batch_size, sequence_length) denoting which positions should be included in the loss.

        loss_normalization: Literal["sequence", "constant"] = "sequence" "sequence": average loss over each sequence, then average over sequences; "constant": normalize total loss by a constant.

        normalization_constant: int | None = None The constant to divide total loss by; required if loss_normalization = "constant".
    
    Returns:
        loss: torch.Tensor A scalar containing the average loss. Make sure you can later call backward on this loss.
    '''
    if loss_normalization == "constant":
        raise NotImplementedError
    ret = 0
    batch_size = mask.shape[0]
    for i in range(batch_size):
        ret += per_token_policy_gradient_loss[i,:][mask[i,:].bool()].mean()
    return ret/batch_size

def grpo_train_step(
    model: PreTrainedModel,
    tokenizer: PreTrainedTokenizer,
    optimizer: Optimizer,
    gradient_accumulation_steps: int,
    max_grad_norm: float | None,
    reward_fn: Callable[[str, str], dict[str, float]],
    repeated_prompts: list[str],
    rollout_responses: list[str],
    repeated_ground_truths: list[str],
    group_size: int,
    # Reward normalization
    baseline: Literal["mean", "none"] = "mean",
    advantage_eps: float = 1e-6,
    advantage_normalizer: Literal["std", "none", "mean"] = "std",
    # Importance reweighting and clipping
    importance_reweighting_method: Literal["none", "noclip", "grpo", "gspo"] = "none",
    old_log_probs: torch.Tensor | None = None,
    cliprange: float | None = None,
    # Loss normalization
    loss_normalization: Literal["sequence", "constant"] = "sequence",
    normalization_constant: int | None = None,
) -> tuple[torch.Tensor, dict[str, torch.Tensor | float]]:
    '''
    Args:
        model: PreTrainedModel HuggingFace model to train.

        tokenizer: PreTrainedTokenizer Tokenizer to use for tokenization.

        optimizer: Optimizer Optimizer for the model.

        gradient_accumulation_steps: int Number of microbatches per optimizer step.

        max_grad_norm: float | None If not None, clip the gradient norm to this value before calling optimizer.step().

        reward_fn: Callable[[str, str], dict[str, float]] Scores the rollout responses against the ground truths, producing a dict with keys "reward", "format_reward", and "answer_reward".

        repeated_prompts: list[str] The prompts for the examples. The length of this list is rollout_batch_size, because the prompt for each example is repeated group_size times.

        rollout_responses: list[str] Rollouts from the policy. The length of this list is rollout_batch_size = n_prompts_per_rollout_batch * group_size.

        repeated_ground_truths: list[str] The ground truths for the examples. The length of this list is rollout_batch_size, because the ground truth for each example is repeated group_size times.

        group_size: int Number of responses per question (group).
        
        baseline: Literal["mean", "none"] If mean, subtract the per-group mean reward; if none, do nothing.

        advantage_eps: float Small constant to avoid division by zero in normalization.

        advantage_normalizer: Literal["std", "none", "mean"] If std, divide by the per-group standard deviation; if none, do nothing; if mean, divide by the per-group mean reward.

        importance_reweighting_method: Literal["none", "noclip", "grpo", "gspo"] "none": no importance reweighting; "noclip": apply importance reweighting without clipping; "grpo": do PPO/GRPO-style token-level reweighting and clipping; "gspo": do GSPO-style sequence-level reweighting and clipping.

        old_log_probs: torch.Tensor | None Required unless importance_reweighting_method = "none"; shape (batch_size, sequence_length).

        cliprange: float | None = None Clip parameter 𝜀, required when importance_reweighting_method is "grpo" or "gspo".

        loss_normalization: Literal["sequence", "constant"] = "sequence" "sequence": average loss over each sequence, then average over sequences; "constant": normalize total loss by a constant (fixed for all of training).

        normalization_constant: int | None = None The constant to divide total loss by; required if loss_normalization = "constant".
    
    Returns:
        tuple[torch.Tensor, dict[str, torch.Tensor]].

        loss scalar tensor. The batch loss, adjusted for gradient accumulation. We return this so we can log it.

        metadata Dict with metadata from the underlying loss call, gradient norm before clipping, and any other statistics you might want to log.
    '''
    if baseline != 'mean' or advantage_normalizer != 'std' or importance_reweighting_method != "none" or loss_normalization != "sequence":
        raise NotImplementedError

    losses = 0
    rollout_batch_size = len(repeated_prompts)
    microbatch_size = rollout_batch_size // gradient_accumulation_steps

    mean_token_entropy = 0
    for i in range(0, rollout_batch_size, microbatch_size):
        # Get the microbatch of prompts and rollouts
        repeated_prompts_microbatch = repeated_prompts[i:i+microbatch_size]
        rollout_responses_microbatch = rollout_responses[i:i+microbatch_size]
        repeated_ground_truths_microbatch = repeated_ground_truths[i:i+microbatch_size]

        tokenized = tokenize_prompt_and_output(
            repeated_prompts_microbatch, 
            rollout_responses_microbatch,
            tokenizer
        )
        input_ids = tokenized['input_ids']
        labels = tokenized['labels']
        response_mask = tokenized['response_mask']

        response_log_probs = get_response_log_probs(
            model, input_ids, labels, return_token_entropy=True
        )
        policy_log_probs = response_log_probs['log_probs']
        token_entropy = response_log_probs['token_entropy']
        mean_token_entropy += (token_entropy.detach().mean(-1).mean() * microbatch_size / rollout_batch_size).item()

        raw_rewards, mean_rewards = compute_rollout_rewards(
            reward_fn, 
            rollout_responses_microbatch, 
            repeated_ground_truths_microbatch
        )
        mean_total_reward = mean_rewards['mean_total_reward']
        mean_format_reward = mean_rewards['mean_format_reward']

        raw_rewards = raw_rewards.to(model.device)
        group_normalized_reward, _ = compute_group_normalized_rewards(
            raw_rewards, 
            group_size, 
            baseline, 
            advantage_eps,
            advantage_normalizer
        )

        per_token_policy_gradient_loss, _ = compute_policy_gradient_loss(
            group_normalized_reward, 
            policy_log_probs,
            importance_reweighting_method,
            old_log_probs,
            cliprange, 
            response_mask
        )
        
        loss = aggregate_loss_across_microbatch(
            per_token_policy_gradient_loss,
            response_mask,
            loss_normalization,
            normalization_constant
        )
        loss.backward()
        losses += loss * microbatch_size / rollout_batch_size

    grads = [p.grad.detach().flatten() for p in model.parameters() if p.grad is not None]
    grad_norm = torch.cat(grads).norm(p=2).item()
    clip_grad_norm_(model.parameters(), max_grad_norm)

    optimizer.step()
    optimizer.zero_grad()
    return losses, mean_total_reward, mean_format_reward, grad_norm, mean_token_entropy

@torch.no_grad()
def grpo_eval(
    model: PreTrainedModel,
    tokenizer: PreTrainedTokenizer,
    reward_fn: Callable[[str, str], dict[str, float]],
    repeated_prompts: list[str],
    rollout_responses: list[str],
    repeated_ground_truths: list[str],
    group_size: int,
    # Reward normalization
    baseline: Literal["mean", "none"] = "mean",
    advantage_eps: float = 1e-6,
    advantage_normalizer: Literal["std", "none", "mean"] = "std",
    # Importance reweighting and clipping
    importance_reweighting_method: Literal["none", "noclip", "grpo", "gspo"] = "none",
    old_log_probs: torch.Tensor | None = None,
    cliprange: float | None = None,
    # Loss normalization
    loss_normalization: Literal["sequence", "constant"] = "sequence",
    normalization_constant: int | None = None,
) -> tuple[torch.Tensor, dict[str, torch.Tensor | float]]:
    if baseline != 'mean' or advantage_normalizer != 'std' or importance_reweighting_method != "none" or loss_normalization != "sequence":
        raise NotImplementedError
    mean_token_entropy = 0
    losses = 0
    rollout_batch_size = len(repeated_prompts)
    microbatch_size = 64
    for i in range(0, rollout_batch_size, microbatch_size):
        # Get the microbatch of prompts and rollouts
        repeated_prompts_microbatch = repeated_prompts[i:i+microbatch_size]
        rollout_responses_microbatch = rollout_responses[i:i+microbatch_size]
        repeated_ground_truths_microbatch = repeated_ground_truths[i:i+microbatch_size]

        tokenized = tokenize_prompt_and_output(
            repeated_prompts_microbatch, 
            rollout_responses_microbatch,
            tokenizer
        )
        input_ids = tokenized['input_ids']
        labels = tokenized['labels']
        response_mask = tokenized['response_mask']

        response_log_probs = get_response_log_probs(
            model, input_ids, labels, return_token_entropy=True
        )
        policy_log_probs = response_log_probs['log_probs']
        token_entropy = response_log_probs['token_entropy']
        mean_token_entropy += (token_entropy.mean(-1).mean() * microbatch_size / rollout_batch_size).item()

        raw_rewards, mean_rewards = compute_rollout_rewards(
            reward_fn, 
            rollout_responses_microbatch, 
            repeated_ground_truths_microbatch
        )
        mean_total_reward = mean_rewards['mean_total_reward']
        mean_format_reward = mean_rewards['mean_format_reward']

        raw_rewards = raw_rewards.to(model.device)
        group_normalized_reward, _ = compute_group_normalized_rewards(
            raw_rewards, 
            group_size, 
            baseline, 
            advantage_eps,
            advantage_normalizer
        )

        group_normalized_reward = group_normalized_reward.to(model.device)
        per_token_policy_gradient_loss, _ = compute_policy_gradient_loss(
            group_normalized_reward, 
            policy_log_probs,
            importance_reweighting_method,
            old_log_probs,
            cliprange, 
            response_mask
        )
        
        loss = aggregate_loss_across_microbatch(
            per_token_policy_gradient_loss,
            response_mask,
            loss_normalization,
            normalization_constant
        )
        losses += loss * microbatch_size / rollout_batch_size
    return losses, mean_total_reward, mean_format_reward, mean_token_entropy

def get_args():
    '''
    Note that rollout_batch_size and train_batch_size count responses, not prompts. So rollout_batch_size = train_batch_size = 256 means 32 prompts with 8 rollouts each.
    I.e., 32 prompts in total, every prompt is sampeld with 8 rollouts.
    '''
    parser = argparse.ArgumentParser(description='CS336 Assignment 5: GRPO training')
    parser.add_argument(
        '--seed',
        default=42,
        type=int
    )
    parser.add_argument(
        '--n_train_examples', 
        type=int, 
        default=6400, 
        help='total number of prompts used for generating rollouts in training'
    )
    parser.add_argument(
        '--n_val_examples',
        type=int,
        default=1024,
        help='number of val examples'
    )
    parser.add_argument(
        '--num_rollout_steps',
        type=int,
        default=200,
        help='number of rollout steps'
    )
    parser.add_argument(
        '--lr',
        type=float,
        default=1e-5,
    )
    parser.add_argument(
        '--rollout_batch_size',
        type=int,
        default=256,
        help='rollout_batch_size = group_size * prompts_per_rollout_step'
    )
    parser.add_argument(
        '--train_batch_size',
        type=int,
        default=256,
        help='same as rollout_batch_size'
    )
    parser.add_argument(
        '--group_size',
        type=int,
        default=8,
        help='every prompt will get group_size number of rollouts'
    )
    parser.add_argument(
        '--gradient_accumulation_steps',
        type=int,
        default=8
    )
    parser.add_argument(
        '--sampling_temperature',
        type=float,
        default=1.0
    )
    parser.add_argument(
        '--sampling_max_tokens',
        type=int,
        default=512
    )
    parser.add_argument(
        '--max_grad_norm',
        type=float,
        default=1.0
    )
    parser.add_argument(
        '--adam-beta1',
        type=float,
        default=0.9
    )
    parser.add_argument(
        '--adam-beta2',
        type=float,
        default=0.95
    )
    parser.add_argument(
        '--weight_decay',
        type=float,
        default=0.0
    )
    parser.add_argument(
        '--train_data_path',
        type=str,
        default='/global/cfs/cdirs/m4410/mzheng/cs336/assignment5-alignment/data/gsm8k/train.jsonl'
    )
    parser.add_argument(
        '--eval_data_path',
        type=str,
        default='/global/cfs/cdirs/m4410/mzheng/cs336/assignment5-alignment/data/gsm8k/test.jsonl'
    )
    parser.add_argument(
        '--wandb_project',
        type=str,
        default='CS336_assignment5'
    )
    parser.add_argument(
        '--wandb_name',
        type=str,
        default='GRPO_GSM8K'
    )
    parser.add_argument(
        '--model_id_or_path',
        type=str,
        default='allenai/OLMo-2-0425-1B'
    )
    parser.add_argument(
        '--eval_frequency',
        type=int,
        default=10,
        help='evaluate on the test dataset every 10 rollout batches'
    )
    parser.add_argument(
        '--log_train_rollouts',
        action='store_true',
        help='if set, will dump the train rollouts every step'
    )
    parser.add_argument(
        '--log_eval_rollouts',
        action='store_true',
        help='if set, will dump the eval rollouts every step'
    )
    return parser.parse_args()

def load_json_dataset(json_path, num_samples):
    ret = []
    with open(json_path, 'r') as f:
        for line in f.readlines():
            if line.strip():
                ret.append(json.loads(line))
            if len(ret) >= num_samples:
                break
    return ret

def dump_prompts_and_rollouts(json_path, prompts_and_rollouts):
    with open(json_path, 'w') as f:
        json.dump(prompts_and_rollouts, f, indent=4)

def compute_avg_rollout_len(responses):
    total_len = 0
    for response in responses:
        total_len += len(response)
    return total_len / len(responses)

def train_GRPO(args):
    if args.log_train_rollouts:
        os.makedirs('./train_rollouts', exist_ok=True)
        os.makedirs(f'./train_rollouts/seed_{args.seed}', exist_ok=True)
        args.train_rollout_path = f'./train_rollouts/seed_{args.seed}'
    if args.log_eval_rollouts:
        os.makedirs('./eval_rollouts', exist_ok=True)
        os.makedirs(f'./eval_rollouts/seed_{args.seed}', exist_ok=True)
        args.eval_rollout_path = f'./eval_rollouts/seed_{args.seed}'

    wandb_enabled = False
    if 'WANDB_API_KEY' in os.environ:
        wandb_enabled = True
    if wandb_enabled:
        wandb.login(key=os.environ['WANDB_API_KEY'])
        wandb.init(
            project=args.wandb_project,
            name=f"{args.wandb_name}_seed_{args.seed}",
            config=args
        )
    server = VLLMServer(
        model_id=args.model_id_or_path,
        gpu=1,
        logging_level='ERROR',
        seed=args.seed
    )
    server.start()
    server.init_weight_sync('cuda:0')

    sampling_params_r1_zero = {
        'temperature': args.sampling_temperature,
        'max_tokens': args.sampling_max_tokens,
        'stop': ["</answer>"],
        'top_p': 1.0,
        'n': args.group_size,
        "include_stop_str_in_output": True,
    }

    prompts_per_rollout_step = args.rollout_batch_size // args.group_size
    num_rollout_steps = args.n_train_examples // prompts_per_rollout_step
    train_prompts = load_json_dataset(args.train_data_path, args.n_train_examples)
    
    val_prompts = load_json_dataset(args.eval_data_path, args.n_val_examples)
    prompts_formatted_eval = reformat(val_prompts, R1_ZERO_PROMPT)
    repeated_ground_truths_eval = extract_gsm8k_ground_truth(val_prompts, args.group_size)
    repeated_prompts_eval = reformat(val_prompts, R1_ZERO_PROMPT, repeat=args.group_size)

    model, tokenizer = get_model_and_tokenizer(args.model_id_or_path, device='cuda:0')
    optimizer = AdamW(model.parameters(), lr=args.lr, betas=(args.adam_beta1, args.adam_beta2), weight_decay=args.weight_decay)
    for step, i in enumerate(range(0, args.n_train_examples, prompts_per_rollout_step)):
        step = step + 1
        model.train()
        prompts_and_answers = train_prompts[i:i+prompts_per_rollout_step]
        repeated_ground_truths = extract_gsm8k_ground_truth(prompts_and_answers, repeat=args.group_size)
        prompts_formatted = reformat(prompts_and_answers, R1_ZERO_PROMPT)
        repeated_prompts_formatted = reformat(prompts_and_answers, R1_ZERO_PROMPT, repeat=args.group_size)
        completions = server.generate_completions(
            prompts=prompts_formatted,
            sampling_params=sampling_params_r1_zero,
        )
        responses = [response.text for response in completions]
        if args.log_train_rollouts:
            train_prompts_and_rollouts = [
                {
                    'question': prompt, 
                    'response': response, 
                    'ground_truth': ground_truth
                } for prompt, response, ground_truth in zip(
                    repeated_prompts_eval, 
                    responses, 
                    repeated_ground_truths
                )
            ]
            dump_prompts_and_rollouts(
                os.path.join(args.train_rollout_path, f'step_{step}.json'), 
                train_prompts_and_rollouts
            )

        loss, mean_total_reward, mean_format_reward, grad_norm, mean_token_entropy = grpo_train_step(
            model=model,
            tokenizer=tokenizer,
            optimizer=optimizer,
            gradient_accumulation_steps=args.gradient_accumulation_steps,
            max_grad_norm=args.max_grad_norm,
            reward_fn=r1_zero_reward_fn,
            repeated_prompts=repeated_prompts_formatted,
            rollout_responses=responses,
            repeated_ground_truths=repeated_ground_truths,
            group_size=args.group_size,
        )
        if wandb_enabled:
            wandb.log({
                "train_loss": loss,
                "mean_total_reward": mean_total_reward,
                "mean_format_reward": mean_format_reward,
                "grad_norm": grad_norm,
                "mean_token_entropy": mean_token_entropy
            }, step=step)
        server.sync_policy_weights(model)
        print(f"[step {step} / {num_rollout_steps}] train loss: {loss.detach().item():.4f} | grad norm: {grad_norm:.4f} | mean_total_reward: {mean_total_reward:.4f}: mean_format_reward: {mean_format_reward:.4f} | mean_token_entropy: {mean_token_entropy:.4f}", flush=True)

        if step % args.eval_frequency == 0:
            model.eval()
            completions = server.generate_completions(
                prompts=prompts_formatted_eval,
                sampling_params=sampling_params_r1_zero
            )
            responses = [response.text for response in completions]
            avg_response_len_eval = compute_avg_rollout_len(responses)
            if args.log_eval_rollouts:
                eval_prompts_and_rollouts = [
                    {
                        'question': prompt, 
                        'response': response, 
                        'ground_truth': ground_truth
                    } for prompt, response, ground_truth in zip(
                        repeated_prompts_eval, 
                        responses, 
                        repeated_ground_truths_eval
                    )
                ]
                dump_prompts_and_rollouts(
                    os.path.join(args.eval_rollout_path, f'step_{step}.json'), 
                    eval_prompts_and_rollouts
                )

            eval_loss, mean_total_reward_eval, mean_format_reward_eval, mean_token_entropy_eval = grpo_eval(
                model=model,
                tokenizer=tokenizer,
                reward_fn=r1_zero_reward_fn,
                repeated_prompts=repeated_prompts_eval,
                rollout_responses=responses,
                repeated_ground_truths=repeated_ground_truths_eval,
                group_size=args.group_size,
            )
            print(f"[step {step} / {num_rollout_steps}] eval loss: {loss.item():.4f} | mean_total_reward_eval: {mean_total_reward_eval:.4f} | mean_format_reward_eval: {mean_format_reward_eval:.4f} | avg_response_len_eval: {avg_response_len_eval:.2f} | mean_token_entropy: {mean_token_entropy_eval:.4f}", flush=True)
            if wandb_enabled:
                wandb.log({
                    "eval_loss": eval_loss,
                    'mean_total_reward_eval': mean_total_reward_eval,
                    'mean_format_reward_eval': mean_format_reward_eval,
                    'avg_response_len_eval': avg_response_len_eval,
                    "mean_token_entropy_eval": mean_token_entropy_eval,
                }, step=step)
    if wandb_enabled:
        wandb.finish()
    server.stop()

if __name__ == '__main__':
    args = get_args()
    train_GRPO(args)