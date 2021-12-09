# The MIT License (MIT)
# Copyright © 2021 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

""" Mixture-of-experts weighting and distillation learning test for template_miner.
"""

import wandb
import torch
import argparse
import bittensor
import transformers

from datasets import load_dataset
from torch import nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM

# from bittensor._neuron.text.template_miner.nucleus_impl import Nucleus
from nucleus_296_11 import Nucleus


def modify_args(parser: argparse.ArgumentParser):
    r""" Modify custom params in the parser for this test.
    """
    parser.add_argument('--wandb.name', type=str, help='''Optionally pass wandb run name for use_wandb''',
                        default='BIT-296-distil-11')
    parser.add_argument('--wandb.project', type=str, help='''Optionally pass wandb project name for use_wandb''',
                        default='neuron-tests')
    parser.add_argument('--wandb.tags', type=str, help='''Optionally pass wandb tags for use_wandb''',
                        default='hf losses, no-pos-enc, neuron, test, template_miner_distil, gpt-j')
    parser.add_argument('--wandb.run_group', type=str, help='''Optionally pass wandb group name for use_wandb''',
                        default='template_miner_distil_gpt-j')

    parser.add_argument('--dataset.batch_size', type=int, help='Batch size.', default=16)
    parser.add_argument('--dataset.block_size', type=int, help='Number of text items to pull for each example..',
                        default=80)
    parser.add_argument('--dataset.num_workers', type=int, help='Number of workers for data loader.', default=6)
    parser.add_argument('--dataset.name', type=str, help='Which dataset to use.', default='bookcorpusopen')
    parser.add_argument('--dataset.split', type=str, help='Which split to use (train/test/validation).',
                        default='train')

    parser.add_argument('--nucleus.nhid', type=int,
                        help='the dimension of the feedforward network model in nn.TransformerEncoder', default=1024)
    parser.add_argument('--nucleus.nhead', type=int, help='the number of heads in the multihead attention models',
                        default=16)
    parser.add_argument('--nucleus.nlayers', type=int,
                        help='the number of nn.TransformerEncoderLayer in nn.TransformerEncoder', default=24)
    parser.add_argument('--nucleus.nlayers_local_hidden', type=int,
                        help='the number of nn.TransformerEncoderLayer in nn.TransformerEncoder', default=2)
    parser.add_argument('--nucleus.nlayers_remote_hidden', type=int,
                        help='the number of nn.TransformerEncoderLayer in nn.TransformerEncoder', default=2)
    parser.add_argument('--nucleus.dropout', type=float, help='the dropout value', default=0.1)

    # From: https://github.com/huggingface/transformers/blob/master/examples/research_projects/distillation/train.py
    parser.add_argument(
        "--nucleus.gradient_accumulation_steps",
        type=int,
        default=2,
        help="Gradient accumulation for larger training batches.",
    )
    parser.add_argument("--nucleus.temperature", default=2.0, type=float,
                        help="Temperature for the softmax temperature.")
    parser.add_argument(
        "--nucleus.alpha_ce", default=0.5, type=float, help="Linear weight for the distillation loss. Must be >=0."
    )
    parser.add_argument("--nucleus.alpha_clm", default=0.5, type=float,
                        help="Linear weight for the CLM loss. Must be >=0.")
    parser.add_argument("--nucleus.alpha_clm_dis", default=0.0, type=float,
                        help="Linear weight for the CLM distillation loss. Must be >=0.")
    parser.add_argument("--nucleus.alpha_mse", default=0.0, type=float,
                        help="Linear weight of the MSE loss. Must be >=0.")
    parser.add_argument("--nucleus.alpha_mse_hid", default=0.0, type=float,
                        help="Linear weight of the hidden MSE loss. Must be >=0.")
    parser.add_argument(
        "--nucleus.alpha_cos", default=0.0, type=float, help="Linear weight of the cosine embedding loss. Must be >=0."
    )

    parser.add_argument('--neuron.learning_rate', type=float, help='Training initial learning rate.', default=1e-4)
    parser.add_argument('--neuron.weight_decay', type=float, help='nucleus parameter weight decay.', default=0.25)
    parser.add_argument('--neuron.momentum', type=float, help='optimizer momentum.', default=0.8)
    parser.add_argument('--neuron.clip_gradients', type=float,
                        help='Implement gradient clipping to avoid exploding loss on smaller architectures.',
                        default=1.0)
    parser.add_argument('--neuron.batch_size_train', type=int, help='Training batch size.', default=16)
    parser.add_argument('--neuron.device', type=str, help='Torch device for training.', default='cuda:1')
    parser.add_argument('--neuron.second_device', type=str, help='Torch second device training.',
                        default='cuda:1')
    parser.add_argument('--neuron.use_wandb', action='store_true',
                        help='''neuron activates its weights and biases powers''', default=False)
    parser.add_argument('--neuron.n_epochs', type=int, help='Number of training epochs.', default=300000)
    parser.add_argument('--neuron.lr_scheduler', type=str, help='Learning rate scheduler name.',
                        default='get_cosine_with_hard_restarts_schedule_with_warmup')
    parser.add_argument('--neuron.num_warmup_steps', type=int, help='Learning rate scheduler number of warmup steps.',
                        default=30000)
    parser.add_argument('--neuron.num_cycles', type=int,
                        help='Learning rate scheduler number of cycles for hard restart.', default=5)
    parser.add_argument('--neuron.learning_rate_chain', type=float, help='Training initial learning rate.', default=1)
    parser.add_argument('--neuron.weight_decay', type=float, help='nucleus parameter weight decay.', default=0.25)
    parser.add_argument('--neuron.momentum', type=float, help='optimizer momentum.', default=0.8)
    parser.add_argument('--neuron.clip_gradients', type=float,
                        help='Implement gradient clipping to avoid exploding loss on smaller architectures.',
                        default=1.0)


def main_config() -> 'bittensor.Config':
    r""" Fills a config namespace object with defaults or information from the command line.
    """
    parser = argparse.ArgumentParser(conflict_handler='resolve')
    parser.add_argument('--config', type=str, help='If set, defaults are overridden by passed file.')

    bittensor.logging.add_args(parser)
    bittensor.wandb.add_args(parser)
    bittensor.dataset.add_args(parser)
    bittensor._neuron.text.template_miner.nucleus.add_args(parser)

    modify_args(parser)

    return bittensor.config(parser)


def chunk(batch, block_size: int):
    r"""
    Concatenates and chunks a batch of token sequences into batches of length block_size.
    Args:
        batch: Input batch of tokenized sequences.
        block_size: Length of each token sequence in the batch.

    Returns:
        A new modified batch of shape [new_batch_size, block_size].
    """
    concatenated = {key: sum(batch[key], []) for key in batch.keys()}
    total_length = len(concatenated['input_ids'])
    trunc_length = (total_length // block_size) * block_size
    new_batch = {
        key: [val[i:i + block_size] for i in range(0, trunc_length, block_size)] for key, val in concatenated.items()
    }
    return new_batch


def main(config: 'bittensor.Config'):
    r"""
    Trains template_miner nucleus local transformer model on a large dataset, with a next token prediction objective.
    Use as test to evaluate next token prediction accuracy by comparing against pretrained model baseline.
    Use as validation check with expectation of similar train/validation accuracy, to ensure no label leak
    in the training process which would produce much larger train accuracy than validation accuracy.
    Args:
        config (:obj:`bittensor.Config`, `required`): bittensor config

    Returns:

    """
    batch_size = config.dataset.batch_size
    block_size = config.dataset.block_size

    # Load a named dataset split from HuggingFace datasets.
    dataset = load_dataset(config.dataset.name, split=config.dataset.split)

    # Tokenize the dataset text sequences.
    tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-j-6B")
    dataset = dataset.map(lambda _batch: tokenizer(_batch['text']), remove_columns=['text', 'title'],
                          batched=True, num_proc=config.dataset.num_workers)

    # Chunk the token sequences into fixed block_size length.
    dataset = dataset.map(lambda _batch: chunk(_batch, block_size),
                          batched=True, batch_size=2, num_proc=config.dataset.num_workers)  #

    # Format our dataset to outputs torch.Tensor to train a pytorch model.
    columns = ['input_ids', 'attention_mask']
    dataset.set_format(type='torch', columns=columns)

    # Define pytorch dataloader with shuffled batches of batch_size token sequences of block_size length.
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    distil_device = config.neuron.device

    # Choose teacher models with significantly different capacity to observe learning of
    #  significantly different peer weights for these by the nucleus distillation model.
    teachers = [{'name': 'EleutherAI/gpt-j-6B',  # 28-layer, 4096-hidden, 16-heads, 2048 n_ctx, 6B parameters
                 'dim': 4096,
                 'device': config.neuron.device}
                ]

    for teacher in teachers:
        # Load pretrained teacher models with language-modeling heads
        teacher['model'] = AutoModelForCausalLM.from_pretrained(teacher['name']).to(teacher['device'])

        # Adapt the teacher hidden dimension to the bittensor network dimension of bittensor by using
        #  a fully-connected layer to convert teacher hidden features to a size of bittensor.__network_dim__
        teacher['adaptor'] = nn.Linear(teacher['dim'], bittensor.__network_dim__, bias=False).to(distil_device)

    # Learn the dimension adaptors for the input teachers getting mixed
    adaptor_params = sum((list(teacher['adaptor'].parameters()) for teacher in teachers), [])
    adaptor_optimizer = torch.optim.AdamW(adaptor_params, lr=config.neuron.learning_rate)

    # Initialize nucleus pytorch model to perform distillation from teacher and move to specified device
    distil_config = config.copy()
    distil_config.neuron.device = distil_device
    distil_config.nucleus.alpha_clm = 1.
    distil_config.nucleus.alpha_clm_dis = 1.
    distil_config.nucleus.alpha_clm_rmt = 1.
    distil_config.nucleus.alpha_mse = 0.0
    distil_config.nucleus.alpha_mse_hid = 1.
    distil_config.nucleus.alpha_ce = 1.
    distil_config.nucleus.alpha_cos = 1.
    distil_model = Nucleus(distil_config).to(distil_device)
    # Accommodate 2 remote teachers for this experiment
    distil_model.peer_weights = nn.Parameter(torch.ones([len(teachers)], requires_grad=True, device=distil_device))
    # Save model to capture unique parameter initialization for reuse in undistil model.
    distil_state = distil_model.state_dict()

    print('distil', distil_model.alpha_ce, distil_model.alpha_clm, distil_model.alpha_clm_dis,
          distil_model.alpha_clm_rmt, distil_model.alpha_mse, distil_model.alpha_mse_hid, distil_model.alpha_cos)

    # Initialize another nucleus that learns an lm head but without distillation
    undistil_device = config.neuron.device
    undistil_config = config.copy()
    undistil_config.neuron.device = undistil_device
    undistil_config.nucleus.alpha_clm = 1.
    undistil_config.nucleus.alpha_clm_dis = 0.0
    undistil_config.nucleus.alpha_clm_rmt = 0.0
    undistil_config.nucleus.alpha_mse = 0.0
    undistil_config.nucleus.alpha_mse_hid = 0.0
    undistil_config.nucleus.alpha_ce = 0.0
    undistil_config.nucleus.alpha_cos = 0.0
    undistil_model = Nucleus(undistil_config)
    # undistil model won't distil, but need to create same-size parameter to load same initialization
    undistil_model.peer_weights = nn.Parameter(torch.ones([len(teachers)], requires_grad=True, device=distil_device))
    # Load same initialization as distil_model
    undistil_model.load_state_dict(distil_state, strict=True)
    undistil_model = undistil_model.to(undistil_device)

    print(undistil_model)
    print('undistil', undistil_model.alpha_ce, undistil_model.alpha_clm, undistil_model.alpha_clm_dis,
          undistil_model.alpha_clm_rmt, undistil_model.alpha_mse, undistil_model.alpha_mse_hid,
          undistil_model.alpha_cos)

    # Original optimizer in template-miner, but learning rate of 1 is too high for this scenario since the adaptors
    #  first need to get trained before teacher capabilities can be discerned.
    # So we opt for using the AdamW with lower learning rate also for the peer weight learning.

    # distil_weight_optimizer = torch.optim.SGD(
    #     [{'params': distil_model.peer_weights,
    #       'lr': distil_model.config.neuron.learning_rate_chain,
    #       'momentum': distil_model.config.neuron.momentum}]
    # )
    # distil_weight_scheduler = torch.optim.lr_scheduler.StepLR(distil_weight_optimizer, step_size=1000, gamma=0.995)

    # print(len(list(distil_model.parameters())), len(list(filter(lambda p: id(p) != id(distil_model.peer_weights), distil_model.parameters()))))
    # Define optimizer over all model parameters at specified learning rate
    # distil_optimizer = torch.optim.AdamW(filter(lambda p: id(p) != id(distil_model.peer_weights), distil_model.parameters()),
    #                                      lr=config.neuron.learning_rate)
    distil_optimizer = torch.optim.AdamW(distil_model.parameters(), lr=config.neuron.learning_rate)
    undistil_optimizer = torch.optim.AdamW(undistil_model.parameters(), lr=config.neuron.learning_rate)

    # Define learning rate scheduler (multiplier) for optimizer
    distil_scheduler = None
    undistil_scheduler = None
    adaptor_scheduler = None

    if config.neuron.lr_scheduler == 'get_cosine_schedule_with_warmup':
        adaptor_scheduler = transformers.get_cosine_schedule_with_warmup(optimizer=adaptor_optimizer,
                                                                         num_warmup_steps=config.neuron.num_warmup_steps,
                                                                         num_training_steps=config.neuron.n_epochs)
        distil_scheduler = transformers.get_cosine_schedule_with_warmup(optimizer=distil_optimizer,
                                                                        num_warmup_steps=config.neuron.num_warmup_steps,
                                                                        num_training_steps=config.neuron.n_epochs)
        undistil_scheduler = transformers.get_cosine_schedule_with_warmup(optimizer=undistil_optimizer,
                                                                          num_warmup_steps=config.neuron.num_warmup_steps,
                                                                          num_training_steps=config.neuron.n_epochs)

    elif config.neuron.lr_scheduler == 'get_cosine_with_hard_restarts_schedule_with_warmup':
        adaptor_scheduler = transformers.get_cosine_with_hard_restarts_schedule_with_warmup(optimizer=adaptor_optimizer,
                                                                                            num_warmup_steps=config.neuron.num_warmup_steps,
                                                                                            num_training_steps=config.neuron.n_epochs,
                                                                                            num_cycles=config.neuron.num_cycles)
        distil_scheduler = transformers.get_cosine_with_hard_restarts_schedule_with_warmup(optimizer=distil_optimizer,
                                                                                           num_warmup_steps=config.neuron.num_warmup_steps,
                                                                                           num_training_steps=config.neuron.n_epochs,
                                                                                           num_cycles=config.neuron.num_cycles)
        undistil_scheduler = transformers.get_cosine_with_hard_restarts_schedule_with_warmup(
            optimizer=undistil_optimizer,
            num_warmup_steps=config.neuron.num_warmup_steps,
            num_training_steps=config.neuron.n_epochs,
            num_cycles=config.neuron.num_cycles)

    if config.neuron.use_wandb:
        bittensor.wandb(config)  # Initialize wandb logging
        wandb.watch(distil_model)  # Track model parameters and gradients
        wandb.watch(undistil_model)  # Track model parameters and gradients
        for teacher in teachers:
            wandb.watch(teacher['adaptor'])
        wandb_table_data = []

    for epoch, batch in enumerate(dataloader):
        with torch.no_grad():
            input_ids = batch['input_ids'].to(distil_device)
            target = input_ids[:, -1]  # held out target of last token
            input_ids = input_ids[:, :-1]  # entire sequence except last token

            teacher_inputs = {}

            for teacher in teachers:
                if teacher['device'] not in teacher_inputs:
                    teacher_inputs[teacher['device']] = input_ids.clone().to(teacher['device'])

                teacher_input_ids = teacher_inputs[teacher['device']]
                teacher_output = teacher['model'](input_ids=teacher_input_ids, output_hidden_states=True)
                teacher['hidden_states'] = teacher_output.hidden_states[-1]

                # Calculate next token prediction accuracy over batch sequences.
                shift_logits = teacher_output.logits[..., :-1, :].contiguous()
                shift_labels = teacher_input_ids[..., 1:].contiguous()
                teacher['loss_clm'] = nn.CrossEntropyLoss()(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
                predictions = shift_logits.detach().max(2).indices
                teacher['acc'] = (predictions == shift_labels).sum().item() / predictions.nelement()

                teacher['prediction'] = teacher_output.logits[:, -1, :].argmax(-1)  # predict unseen last token
                teacher['predictions'] = tokenizer.decode(teacher_output.logits[0].argmax(-1).detach())

                teacher_target = target.clone().to(teacher['device'])
                teacher['target_acc'] = (teacher['prediction'] == teacher_target).sum().item() / len(teacher_target)

            adaptor_lr = adaptor_optimizer.param_groups[0]['lr']

        # Weighted joining of teachers with weights that also get learned
        joining_weights = F.softmax(distil_model.peer_weights, dim=0)
        distil_teacher_inputs = None
        for i, teacher in enumerate(teachers):
            if distil_teacher_inputs is None:
                distil_teacher_inputs = joining_weights[i] * teacher['adaptor'](
                    teacher['hidden_states'].detach().to(distil_device))
            else:
                distil_teacher_inputs += joining_weights[i] * teacher['adaptor'](
                    teacher['hidden_states'].detach().to(distil_device))

        distil_output = distil_model.remote_forward(input_ids, training=True,
                                                    teacher_inputs=distil_teacher_inputs)  # forward pass in local transformer model
        distil_total_loss = (distil_model.alpha_clm * distil_output.loss_clm +
                             distil_model.alpha_clm_dis * distil_output.loss_clm_dis +
                             distil_model.alpha_clm_rmt * distil_output.loss_clm_rmt +
                             distil_model.alpha_mse * distil_output.loss_mse +
                             distil_model.alpha_mse_hid * distil_output.loss_mse_hid +
                             distil_model.alpha_ce * distil_output.loss_ce +
                             distil_model.alpha_cos * distil_output.loss_cos)

        with torch.no_grad():
            distil_loss_clm = distil_output.loss_clm
            distil_loss_clm_dis = distil_output.loss_clm_dis
            distil_loss_clm_rmt = distil_output.loss_clm_rmt
            distil_loss_mse = distil_output.loss_mse
            distil_loss_mse_hid = distil_output.loss_mse_hid
            distil_loss_ce = distil_output.loss_ce
            distil_loss_cos = distil_output.loss_cos
            distil_acc = distil_output.local_accuracy  # training accuracy on next token prediction in train sequence with masking
            distil_remote_acc = distil_output.remote_accuracy  # training accuracy on next token prediction in train sequence with masking
            distil_lr = distil_optimizer.param_groups[0]['lr']  # record actual learning rate
            # distil_weight_lr = distil_weight_optimizer.param_groups[0]['lr']  # record actual learning rate

            distil_prediction = distil_output.local_target[:, -1, :].argmax(-1)  # predict unseen last token
            distil_target_acc = (distil_prediction == target).sum().item() / len(
                target)  # validation accuracy on predicting unseen token

            distil_remote_prediction = distil_output.remote_target[:, -1, :].argmax(-1)  # predict unseen last token
            distil_remote_target_acc = (distil_remote_prediction == target).sum().item() / len(
                target)  # validation accuracy on predicting unseen token

            undistil_input_ids = input_ids.detach().to(undistil_device)

        undistil_output = undistil_model.local_forward(undistil_input_ids,
                                                       training=True)  # forward pass in local transformer model
        undistil_loss = undistil_output.loss_clm

        with torch.no_grad():
            undistil_acc = undistil_output.local_accuracy  # training accuracy on next token prediction in train sequence with masking
            undistil_lr = undistil_optimizer.param_groups[0]['lr']  # record actual learning rate
            undistil_prediction = undistil_output.local_target[:, -1, :].argmax(-1)  # predict unseen last token
            undistil_target = target.to(undistil_device)
            undistil_target_acc = (undistil_prediction == undistil_target).sum().item() / len(
                undistil_target)  # validation accuracy on predicting unseen token

            if epoch % 100 == 0:
                print('%d: %.1f %.1f %.1f '
                      '(%.2f, %.2f, %.2f, '
                      '%.2f, %.2f, %f)' % (epoch, distil_total_loss.item(),
                                           undistil_loss.item(),
                                           distil_acc, undistil_acc,
                                           distil_target_acc, distil_remote_target_acc,
                                           teachers[-1]['target_acc'], undistil_target_acc,
                                           distil_lr), end=' ')

            if epoch % 1000 == 0:
                input_decoded = tokenizer.decode(input_ids[0])
                distil_predictions = distil_output.local_target[0].detach().argmax(-1)
                undistil_predictions = undistil_output.local_target[0].detach().argmax(-1)

                print('\n.\n', input_decoded, '\n...\n')
                print(list(zip([tokenizer.decode(_) for _ in input_ids[0]],
                               [tokenizer.decode(_) for _ in distil_predictions])), '\n.\n')

                distil_predictions = tokenizer.decode(distil_predictions)
                undistil_predictions = tokenizer.decode(undistil_predictions)
                if config.neuron.use_wandb:
                    wandb_table_data += [[epoch,
                                          distil_target_acc,
                                          distil_predictions, undistil_predictions, input_decoded] +
                                         [teacher['predictions'] for teacher in teachers]]

            if config.neuron.use_wandb:
                if epoch % 5000 == 0:
                    wandb_table = wandb.Table(columns=['epoch',
                                                       'distil_target_acc',
                                                       'distil_predictions', 'undistil_predictions', 'input'] +
                                                      ['%s' % teacher['name'] for teacher in teachers])
                    for row in wandb_table_data:
                        wandb_table.add_data(*row)
                    wandb.log({'training_samples': wandb_table})

                    torch.save(distil_model.state_dict(), "{}/distil_model_9.torch".format(config.wandb.directory))

                wandb_log = {'distil_loss_clm': distil_loss_clm.item(),
                             'distil_loss_clm_dis': distil_loss_clm_dis.item(),
                             'distil_loss_clm_rmt': distil_loss_clm_rmt.item(),
                             'distil_loss_mse': distil_loss_mse.item(),
                             'distil_loss_mse_hid': distil_loss_mse_hid.item(),
                             'distil_loss_ce': distil_loss_ce.item(),
                             'distil_loss_cos': distil_loss_cos.item(),
                             'distil_total_loss': distil_total_loss.item(),
                             'distil_acc': distil_acc,
                             'distil_target_acc': distil_target_acc,
                             'distil_remote_target_acc': distil_remote_target_acc,
                             'distil_remote_acc': distil_remote_acc,
                             'distil_lr': distil_lr,
                             'adaptor_lr': adaptor_lr,

                             'undistil_loss': undistil_loss.item(),
                             'undistil_acc': undistil_acc,
                             'undistil_target_acc': undistil_target_acc,
                             'undistil_lr': undistil_lr}

                wandb_log = [wandb_log] + [{'teacher%d_weight' % i: distil_model.peer_weights[i].item(),
                                            'teacher%d_loss_clm' % i: teacher['loss_clm'].item(),
                                            'teacher%d_acc' % i: teacher['acc'],
                                            'teacher%d_target_acc' % i: teacher['target_acc'],
                                            } for i, teacher in enumerate(teachers)]

                wandb.log({k: v for d in wandb_log for k, v in d.items()})

        torch.cuda.empty_cache()

        distil_total_loss.backward()  # accumulate gradients wrt training loss
        undistil_loss.backward()  # accumulate gradients wrt training loss

        if epoch % config.nucleus.gradient_accumulation_steps == 0:
            torch.nn.utils.clip_grad_norm_(distil_model.parameters(), 0.5)
            distil_optimizer.step()  # update model parameters to reduce loss
            distil_optimizer.zero_grad()  # remove previously accumulated gradients
            if distil_scheduler:
                distil_scheduler.step()  # update learning rate multiplier

            # Unused: opting for using main optimizer for peer-weights also
            # distil_weight_optimizer.step()  # update model parameters to reduce loss
            # distil_weight_optimizer.zero_grad()  # remove previously accumulated gradients
            # distil_weight_scheduler.step()

            adaptor_optimizer.step()
            adaptor_optimizer.zero_grad()
            adaptor_scheduler.step()

            torch.nn.utils.clip_grad_norm_(undistil_model.parameters(), 0.5)
            undistil_optimizer.step()  # update model parameters to reduce loss
            undistil_optimizer.zero_grad()  # remove previously accumulated gradients
            if undistil_scheduler:
                undistil_scheduler.step()  # update learning rate multiplier

        torch.cuda.empty_cache()


if __name__ == '__main__':
    use_config = main_config()
    main(use_config)
