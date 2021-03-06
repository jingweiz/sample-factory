import glob
import os
import shutil
import signal
import threading
import time
from collections import OrderedDict, deque
from os.path import join
from queue import Empty, Queue, Full
from threading import Thread

import numpy as np
import psutil
import torch
from torch.multiprocessing import Process, Event as MultiprocessingEvent

import faster_fifo
from algorithms.appo.appo_utils import TaskType, list_of_dicts_to_dict_of_lists, memory_stats, cuda_envvars, \
    TensorBatcher, iter_dicts_recursively, copy_dict_structure, ObjectPool
from algorithms.appo.model import create_actor_critic
from algorithms.appo.population_based_training import PbtTask
from algorithms.utils.action_distributions import get_action_distribution
from algorithms.utils.algo_utils import calculate_gae, EPS
from algorithms.utils.pytorch_utils import to_scalar
from utils.decay import LinearDecay
from utils.timing import Timing
from utils.utils import log, AttrDict, experiment_dir, ensure_dir_exists, join_or_kill, safe_get


class LearnerWorker:
    def __init__(
        self, worker_idx, policy_id, cfg, obs_space, action_space, report_queue, policy_worker_queues, shared_buffers,
        policy_lock, resume_experience_collection_cv,
    ):
        log.info('Initializing the learner %d for policy %d', worker_idx, policy_id)

        self.worker_idx = worker_idx
        self.policy_id = policy_id

        self.cfg = cfg

        # PBT-related stuff
        self.should_save_model = True  # set to true if we need to save the model to disk on the next training iteration
        self.load_policy_id = None  # non-None when we need to replace our parameters with another policy's parameters
        self.pbt_mutex = threading.Lock()
        self.new_cfg = None  # non-None when we need to update the learning hyperparameters

        self.terminate = False

        self.obs_space = obs_space
        self.action_space = action_space

        self.rollout_tensors = shared_buffers.tensor_trajectories
        self.traj_tensors_available = shared_buffers.is_traj_tensor_available
        self.policy_versions = shared_buffers.policy_versions
        self.stop_experience_collection = shared_buffers.stop_experience_collection

        self.stop_experience_collection_num_msgs = self.resume_experience_collection_num_msgs = 0

        self.device = None
        self.actor_critic = None
        self.optimizer = None
        self.policy_lock = policy_lock
        self.resume_experience_collection_cv = resume_experience_collection_cv

        self.task_queue = faster_fifo.Queue()
        self.report_queue = report_queue

        self.initialized_event = MultiprocessingEvent()
        self.initialized_event.clear()

        self.model_saved_event = MultiprocessingEvent()
        self.model_saved_event.clear()

        # queues corresponding to policy workers using the same policy
        # we send weight updates via these queues
        self.policy_worker_queues = policy_worker_queues

        self.experience_buffer_queue = Queue()

        self.tensor_batch_pool = ObjectPool()
        self.tensor_batcher = TensorBatcher(self.tensor_batch_pool)

        self.with_training = True  # set to False for debugging no-training regime
        self.train_in_background = self.cfg.train_in_background_thread  # set to False for debugging

        self.training_thread = Thread(target=self._train_loop) if self.train_in_background else None
        self.train_thread_initialized = threading.Event()

        self.is_training = False

        self.train_step = self.env_steps = 0

        # decay rate at which summaries are collected
        # save summaries every 20 seconds in the beginning, but decay to every 4 minutes in the limit, because we
        # do not need frequent summaries for longer experiments
        self.summary_rate_decay_seconds = LinearDecay([(0, 20), (100000, 120), (1000000, 240)])
        self.last_summary_time = 0

        self.last_saved_time = self.last_milestone_time = 0

        self.discarded_experience_over_time = deque([], maxlen=30)
        self.discarded_experience_timer = time.time()
        self.num_discarded_rollouts = 0

        self.process = Process(target=self._run, daemon=True)

    def start_process(self):
        self.process.start()

    def _init(self):
        log.info('Waiting for the learner to initialize...')
        self.train_thread_initialized.wait()
        log.info('Learner %d initialized', self.worker_idx)
        self.initialized_event.set()

    def _terminate(self):
        self.terminate = True

    def _broadcast_model_weights(self):
        state_dict = self.actor_critic.state_dict()
        policy_version = self.train_step
        log.debug('Broadcast model weights for model version %d', policy_version)
        model_state = (policy_version, state_dict)
        for q in self.policy_worker_queues:
            q.put((TaskType.INIT_MODEL, model_state))

    def _calculate_gae(self, buffer):
        """
        Calculate advantages using Generalized Advantage Estimation.
        This is leftover the from previous version of the algorithm.
        Perhaps should be re-implemented in PyTorch tensors, similar to V-trace for uniformity.
        """

        rewards = torch.stack(buffer.rewards).numpy().squeeze()  # [E, T]
        dones = torch.stack(buffer.dones).numpy().squeeze()  # [E, T]
        values_arr = torch.stack(buffer.values).numpy().squeeze()  # [E, T]

        # calculating fake values for the last step in the rollout
        # this will make sure that advantage of the very last action is always zero
        values = []
        for i in range(len(values_arr)):
            last_value, last_reward = values_arr[i][-1], rewards[i, -1]
            next_value = (last_value - last_reward) / self.cfg.gamma
            values.append(list(values_arr[i]))
            values[i].append(float(next_value))  # [T] -> [T+1]

        # calculating returns and GAE
        rewards = rewards.transpose((1, 0))  # [E, T] -> [T, E]
        dones = dones.transpose((1, 0))  # [E, T] -> [T, E]
        values = np.asarray(values).transpose((1, 0))  # [E, T+1] -> [T+1, E]

        advantages, returns = calculate_gae(rewards, dones, values, self.cfg.gamma, self.cfg.gae_lambda)

        # transpose tensors back to [E, T] before creating a single experience buffer
        buffer.advantages = advantages.transpose((1, 0))  # [T, E] -> [E, T]
        buffer.returns = returns.transpose((1, 0))  # [T, E] -> [E, T]
        buffer.returns = buffer.returns[:, :, np.newaxis]  # [E, T] -> [E, T, 1]

        buffer.advantages = [torch.tensor(buffer.advantages).reshape(-1)]
        buffer.returns = [torch.tensor(buffer.returns).reshape(-1)]

        return buffer

    def _mark_rollout_buffer_free(self, rollout):
        r = rollout
        self.traj_tensors_available[r.worker_idx, r.split_idx][r.env_idx, r.agent_idx, r.traj_buffer_idx] = 1

    def _prepare_train_buffer(self, rollouts, macro_batch_size, timing):
        trajectories = [AttrDict(r['t']) for r in rollouts]

        with timing.add_time('buffers'):
            buffer = AttrDict()

            # by the end of this loop the buffer is a dictionary containing lists of numpy arrays
            for i, t in enumerate(trajectories):
                for key, x in t.items():
                    if key not in buffer:
                        buffer[key] = []
                    buffer[key].append(x)

            # convert lists of dict observations to a single dictionary of lists
            for key, x in buffer.items():
                if isinstance(x[0], (dict, OrderedDict)):
                    buffer[key] = list_of_dicts_to_dict_of_lists(x)

        if not self.cfg.with_vtrace:
            with timing.add_time('calc_gae'):
                buffer = self._calculate_gae(buffer)

        with timing.add_time('batching'):
            # concatenate rollouts from different workers into a single batch efficiently
            # that is, if we already have memory for the buffers allocated, we can just copy the data into
            # existing cached tensors instead of creating new ones. This is a performance optimization.
            use_pinned_memory = self.cfg.device == 'gpu'
            buffer = self.tensor_batcher.cat(buffer, macro_batch_size, use_pinned_memory, timing)

        with timing.add_time('buff_ready'):
            for r in rollouts:
                self._mark_rollout_buffer_free(r)

        with timing.add_time('tensors_gpu_float'):
            device_buffer = self._copy_train_data_to_device(buffer)

        with timing.add_time('squeeze'):
            # will squeeze actions only in simple categorical case
            tensors_to_squeeze = ['actions', 'log_prob_actions', 'policy_version', 'values', 'rewards', 'dones']
            for tensor_name in tensors_to_squeeze:
                device_buffer[tensor_name].squeeze_()

        # we no longer need the cached buffer, and can put it back into the pool
        self.tensor_batch_pool.put(buffer)
        return device_buffer

    def _macro_batch_size(self, batch_size):
        return self.cfg.num_batches_per_iteration * batch_size

    def _process_macro_batch(self, rollouts, batch_size, timing):
        macro_batch_size = self._macro_batch_size(batch_size)

        assert macro_batch_size % self.cfg.rollout == 0
        assert self.cfg.rollout % self.cfg.recurrence == 0
        assert macro_batch_size % self.cfg.recurrence == 0

        samples = env_steps = 0
        for rollout in rollouts:
            samples += rollout['length']
            env_steps += rollout['env_steps']

        with timing.add_time('prepare'):
            buffer = self._prepare_train_buffer(rollouts, macro_batch_size, timing)
            self.experience_buffer_queue.put((buffer, batch_size, samples, env_steps))

    def _process_rollouts(self, rollouts, timing):
        # batch_size can potentially change through PBT, so we should keep it the same and pass it around
        # using function arguments, instead of using global self.cfg

        batch_size = self.cfg.batch_size
        rollouts_in_macro_batch = self._macro_batch_size(batch_size) // self.cfg.rollout

        if len(rollouts) < rollouts_in_macro_batch:
            return rollouts

        discard_rollouts = 0
        policy_version = self.train_step
        for r in rollouts:
            rollout_min_version = r['t']['policy_version'].min().item()
            if policy_version - rollout_min_version >= self.cfg.max_policy_lag:
                discard_rollouts += 1
                self._mark_rollout_buffer_free(r)
            else:
                break

        if discard_rollouts > 0:
            log.warning(
                'Discarding %d old rollouts, cut by policy lag threshold %d (learner %d)',
                discard_rollouts, self.cfg.max_policy_lag, self.policy_id,
            )
            rollouts = rollouts[discard_rollouts:]
            self.num_discarded_rollouts += discard_rollouts

        if len(rollouts) >= rollouts_in_macro_batch:
            # process newest rollouts
            rollouts_to_process = rollouts[:rollouts_in_macro_batch]
            rollouts = rollouts[rollouts_in_macro_batch:]

            self._process_macro_batch(rollouts_to_process, batch_size, timing)
            # log.info('Unprocessed rollouts: %d (%d samples)', len(rollouts), len(rollouts) * self.cfg.rollout)

        return rollouts

    def _get_minibatches(self, batch_size, experience_size):
        """Generating minibatches for training."""
        assert self.cfg.rollout % self.cfg.recurrence == 0
        assert experience_size % batch_size == 0, f'experience size: {experience_size}, batch size: {batch_size}'

        if self.cfg.num_batches_per_iteration == 1:
            return [None]  # single minibatch is actually the entire buffer, we don't need indices

        # indices that will start the mini-trajectories from the same episode (for bptt)
        indices = np.arange(0, experience_size, self.cfg.recurrence)
        indices = np.random.permutation(indices)

        # complete indices of mini trajectories, e.g. with recurrence==4: [4, 16] -> [4, 5, 6, 7, 16, 17, 18, 19]
        indices = [np.arange(i, i + self.cfg.recurrence) for i in indices]
        indices = np.concatenate(indices)

        assert len(indices) == experience_size

        num_minibatches = experience_size // batch_size
        minibatches = np.split(indices, num_minibatches)
        return minibatches

    @staticmethod
    def _get_minibatch(buffer, indices):
        if indices is None:
            # handle the case of a single batch, where the entire buffer is a minibatch
            return buffer

        mb = AttrDict()

        for item, x in buffer.items():
            if isinstance(x, (dict, OrderedDict)):
                mb[item] = AttrDict()
                for key, x_elem in x.items():
                    mb[item][key] = x_elem[indices]
            else:
                mb[item] = x[indices]

        return mb

    def _should_save_summaries(self):
        summaries_every_seconds = self.summary_rate_decay_seconds.at(self.train_step)
        if time.time() - self.last_summary_time < summaries_every_seconds:
            return False

        return True

    def _after_optimizer_step(self):
        """A hook to be called after each optimizer step."""
        self.train_step += 1
        self._maybe_save()

    def _maybe_save(self):
        if time.time() - self.last_saved_time >= self.cfg.save_every_sec or self.should_save_model:
            self._save()
            self.model_saved_event.set()
            self.should_save_model = False
            self.last_saved_time = time.time()

    @staticmethod
    def checkpoint_dir(cfg, policy_id):
        checkpoint_dir = join(experiment_dir(cfg=cfg), f'checkpoint_p{policy_id}')
        return ensure_dir_exists(checkpoint_dir)

    @staticmethod
    def get_checkpoints(checkpoints_dir):
        checkpoints = glob.glob(join(checkpoints_dir, 'checkpoint_*'))
        return sorted(checkpoints)

    def _get_checkpoint_dict(self):
        checkpoint = {
            'train_step': self.train_step,
            'env_steps': self.env_steps,
            'model': self.actor_critic.state_dict(),
            'optimizer': self.optimizer.state_dict(),
        }
        return checkpoint

    def _save(self):
        checkpoint = self._get_checkpoint_dict()
        assert checkpoint is not None

        checkpoint_dir = self.checkpoint_dir(self.cfg, self.policy_id)
        tmp_filepath = join(checkpoint_dir, '.temp_checkpoint')
        checkpoint_name = f'checkpoint_{self.train_step:09d}_{self.env_steps}.pth'
        filepath = join(checkpoint_dir, checkpoint_name)
        log.info('Saving %s...', tmp_filepath)
        torch.save(checkpoint, tmp_filepath)
        log.info('Renaming %s to %s', tmp_filepath, filepath)
        os.rename(tmp_filepath, filepath)

        while len(self.get_checkpoints(checkpoint_dir)) > self.cfg.keep_checkpoints:
            oldest_checkpoint = self.get_checkpoints(checkpoint_dir)[0]
            if os.path.isfile(oldest_checkpoint):
                log.debug('Removing %s', oldest_checkpoint)
                os.remove(oldest_checkpoint)

        if self.cfg.save_milestones_sec > 0:
            # milestones enabled
            if time.time() - self.last_milestone_time >= self.cfg.save_milestones_sec:
                milestones_dir = ensure_dir_exists(join(checkpoint_dir, 'milestones'))
                milestone_path = join(milestones_dir, f'{checkpoint_name}.milestone')
                log.debug('Saving a milestone %s', milestone_path)
                shutil.copy(filepath, milestone_path)
                self.last_milestone_time = time.time()

    @staticmethod
    def _policy_loss(ratio, adv, clip_ratio_low, clip_ratio_high):
        clipped_ratio = torch.clamp(ratio, clip_ratio_low, clip_ratio_high)
        loss_unclipped = ratio * adv
        loss_clipped = clipped_ratio * adv
        loss = torch.min(loss_unclipped, loss_clipped)
        loss = -loss.mean()

        return loss

    def _value_loss(self, new_values, old_values, target, clip_value):
        value_clipped = old_values + torch.clamp(new_values - old_values, -clip_value, clip_value)
        value_original_loss = (new_values - target).pow(2)
        value_clipped_loss = (value_clipped - target).pow(2)
        value_loss = torch.max(value_original_loss, value_clipped_loss)
        value_loss = value_loss.mean()
        value_loss *= self.cfg.value_loss_coeff

        return value_loss

    def _prepare_observations(self, obs_tensors, gpu_buffer_obs):
        for d, gpu_d, k, v, _ in iter_dicts_recursively(obs_tensors, gpu_buffer_obs):
            device, dtype = self.actor_critic.device_and_type_for_input_tensor(k)
            tensor = v.detach().to(device, copy=True).type(dtype)
            gpu_d[k] = tensor

    def _copy_train_data_to_device(self, buffer):
        device_buffer = copy_dict_structure(buffer)

        for key, item in buffer.items():
            if key == 'obs':
                self._prepare_observations(item, device_buffer['obs'])
            else:
                device_tensor = item.detach().to(self.device, copy=True, non_blocking=True)
                device_buffer[key] = device_tensor.float()

        return device_buffer

    def _train(self, gpu_buffer, batch_size, experience_size, timing):
        with torch.no_grad():
            early_stopping_tolerance = 1e-6
            early_stop = False
            prev_epoch_actor_loss = 1e9
            epoch_actor_losses = []

            # V-trace parameters
            # noinspection PyArgumentList
            rho_hat = torch.Tensor([self.cfg.vtrace_rho])
            # noinspection PyArgumentList
            c_hat = torch.Tensor([self.cfg.vtrace_c])

            clip_ratio_high = 1.0 + self.cfg.ppo_clip_ratio  # e.g. 1.1
            # this still works with e.g. clip_ratio = 2, while PPO's 1-r would give negative ratio
            clip_ratio_low = 1.0 / clip_ratio_high

            clip_value = self.cfg.ppo_clip_value
            gamma = self.cfg.gamma
            recurrence = self.cfg.recurrence

            if self.cfg.with_vtrace:
                assert recurrence == self.cfg.rollout and recurrence > 1, \
                    'V-trace requires to recurrence and rollout to be equal'

            num_sgd_steps = 0

            stats_and_summaries = None
            if not self.with_training:
                return stats_and_summaries

        for epoch in range(self.cfg.ppo_epochs):
            with timing.add_time('epoch_init'):
                if early_stop or self.terminate:
                    break

                summary_this_epoch = force_summaries = False

                minibatches = self._get_minibatches(batch_size, experience_size)

            for batch_num in range(len(minibatches)):
                with timing.add_time('minibatch_init'):
                    indices = minibatches[batch_num]

                    # current minibatch consisting of short trajectory segments with length == recurrence
                    mb = self._get_minibatch(gpu_buffer, indices)

                # calculate policy head outside of recurrent loop
                with timing.add_time('forward_head'):
                    head_outputs = self.actor_critic.forward_head(mb.obs)

                # initial rnn states
                with timing.add_time('bptt_initial'):
                    rnn_states = mb.rnn_states[::recurrence]
                    is_same_episode = 1.0 - mb.dones.unsqueeze(dim=1)

                # calculate RNN outputs for each timestep in a loop
                with timing.add_time('bptt'):
                    core_outputs = []
                    for i in range(recurrence):
                        # indices of head outputs corresponding to the current timestep
                        step_head_outputs = head_outputs[i::recurrence]

                        with timing.add_time('bptt_forward_core'):
                            core_output, rnn_states = self.actor_critic.forward_core(step_head_outputs, rnn_states)
                            core_outputs.append(core_output)

                        if self.cfg.use_rnn:
                            # zero-out RNN states on the episode boundary
                            with timing.add_time('bptt_rnn_states'):
                                is_same_episode_step = is_same_episode[i::recurrence]
                                rnn_states = rnn_states * is_same_episode_step

                with timing.add_time('tail'):
                    # transform core outputs from [T, Batch, D] to [Batch, T, D] and then to [Batch x T, D]
                    # which is the same shape as the minibatch
                    core_outputs = torch.stack(core_outputs)

                    num_timesteps, num_trajectories = core_outputs.shape[:2]
                    assert num_timesteps == recurrence
                    assert num_timesteps * num_trajectories == batch_size
                    core_outputs = core_outputs.transpose(0, 1).reshape(-1, *core_outputs.shape[2:])
                    assert core_outputs.shape[0] == head_outputs.shape[0]

                    # calculate policy tail outside of recurrent loop
                    result = self.actor_critic.forward_tail(core_outputs, with_action_distribution=True)

                    action_distribution = result.action_distribution
                    log_prob_actions = action_distribution.log_prob(mb.actions)
                    ratio = torch.exp(log_prob_actions - mb.log_prob_actions)  # pi / pi_old

                    # super large/small values can cause numerical problems and are probably noise anyway
                    ratio = torch.clamp(ratio, 0.05, 20.0)

                    values = result.values.squeeze()

                with torch.no_grad():  # these computations are not the part of the computation graph
                    if self.cfg.with_vtrace:
                        ratios_cpu = ratio.cpu()
                        values_cpu = values.cpu()
                        rewards_cpu = mb.rewards.cpu()  # we only need this on CPU, potential minor optimization
                        dones_cpu = mb.dones.cpu()

                        vtrace_rho = torch.min(rho_hat, ratios_cpu)
                        vtrace_c = torch.min(c_hat, ratios_cpu)

                        vs = torch.zeros((num_trajectories * recurrence))
                        adv = torch.zeros((num_trajectories * recurrence))

                        next_values = (values_cpu[recurrence - 1::recurrence] - rewards_cpu[recurrence - 1::recurrence]) / gamma
                        next_vs = next_values

                        with timing.add_time('vtrace'):
                            for i in reversed(range(self.cfg.recurrence)):
                                rewards = rewards_cpu[i::recurrence]
                                dones = dones_cpu[i::recurrence]
                                not_done = 1.0 - dones
                                not_done_times_gamma = not_done * gamma

                                curr_values = values_cpu[i::recurrence]
                                curr_vtrace_rho = vtrace_rho[i::recurrence]
                                curr_vtrace_c = vtrace_c[i::recurrence]

                                delta_s = curr_vtrace_rho * (rewards + not_done_times_gamma * next_values - curr_values)
                                adv[i::recurrence] = curr_vtrace_rho * (rewards + not_done_times_gamma * next_vs - curr_values)
                                next_vs = curr_values + delta_s + not_done_times_gamma * curr_vtrace_c * (next_vs - next_values)
                                vs[i::recurrence] = next_vs

                                next_values = curr_values

                        targets = vs
                    else:
                        # using regular GAE
                        adv = mb.advantages
                        targets = mb.returns

                    adv_mean = adv.mean()
                    adv_std = adv.std()
                    adv = (adv - adv_mean) / max(1e-3, adv_std)  # normalize advantage
                    adv = adv.to(self.device)

                with timing.add_time('losses'):
                    policy_loss = self._policy_loss(ratio, adv, clip_ratio_low, clip_ratio_high)

                    entropy = action_distribution.entropy()
                    if self.cfg.entropy_loss_coeff > 0.0:
                        entropy_loss = -self.cfg.entropy_loss_coeff * entropy.mean()
                    else:
                        entropy_loss = 0.0

                    actor_loss = policy_loss + entropy_loss
                    epoch_actor_losses.append(actor_loss.item())

                    targets = targets.to(self.device)
                    old_values = mb.values
                    value_loss = self._value_loss(values, old_values, targets, clip_value)
                    critic_loss = value_loss

                    loss = actor_loss + critic_loss

                    high_loss = 30.0
                    if abs(to_scalar(policy_loss)) > high_loss or abs(to_scalar(value_loss)) > high_loss or abs(to_scalar(entropy_loss)) > high_loss:
                        log.warning(
                            'High loss value: %.4f %.4f %.4f %.4f',
                            to_scalar(loss), to_scalar(policy_loss), to_scalar(value_loss), to_scalar(entropy_loss),
                        )
                        force_summaries = True

                with timing.add_time('update'):
                    # update the weights
                    self.optimizer.zero_grad()
                    loss.backward()

                    if self.cfg.max_grad_norm > 0.0:
                        with timing.add_time('clip'):
                            torch.nn.utils.clip_grad_norm_(self.actor_critic.parameters(), self.cfg.max_grad_norm)

                    curr_policy_version = self.train_step  # policy version before the weight update
                    with self.policy_lock:
                        self.optimizer.step()

                    num_sgd_steps += 1

                with torch.no_grad():
                    with timing.add_time('after_optimizer'):
                        self._after_optimizer_step()

                        # collect and report summaries
                        with_summaries = self._should_save_summaries() or force_summaries
                        if with_summaries and not summary_this_epoch:
                            stats_and_summaries = self._record_summaries(AttrDict(locals()))
                            summary_this_epoch = True
                            force_summaries = False

            # end of an epoch
            # this will force policy update on the inference worker (policy worker)
            self.policy_versions[self.policy_id] = self.train_step

            new_epoch_actor_loss = np.mean(epoch_actor_losses)
            loss_delta_abs = abs(prev_epoch_actor_loss - new_epoch_actor_loss)
            if loss_delta_abs < early_stopping_tolerance:
                early_stop = True
                log.debug(
                    'Early stopping after %d epochs (%d sgd steps), loss delta %.7f',
                    epoch + 1, num_sgd_steps, loss_delta_abs,
                )
                break

            prev_epoch_actor_loss = new_epoch_actor_loss
            epoch_actor_losses = []

        return stats_and_summaries

    def _record_summaries(self, train_loop_vars):
        var = train_loop_vars

        self.last_summary_time = time.time()
        stats = AttrDict()

        grad_norm = sum(
            p.grad.data.norm(2).item() ** 2
            for p in self.actor_critic.parameters()
            if p.grad is not None
        ) ** 0.5
        stats.grad_norm = grad_norm
        stats.loss = var.loss
        stats.value = var.result.values.mean()
        stats.entropy = var.action_distribution.entropy().mean()
        stats.policy_loss = var.policy_loss
        stats.value_loss = var.value_loss
        stats.entropy_loss = var.entropy_loss
        stats.adv_min = var.adv.min()
        stats.adv_max = var.adv.max()
        stats.adv_std = var.adv_std
        stats.max_abs_logprob = torch.abs(var.mb.action_logits).max()

        if hasattr(var.action_distribution, 'summaries'):
            stats.update(var.action_distribution.summaries())

        if var.epoch == self.cfg.ppo_epochs - 1 and var.batch_num == len(var.minibatches) - 1:
            # we collect these stats only for the last PPO batch, or every time if we're only doing one batch, IMPALA-style
            ratio_mean = torch.abs(1.0 - var.ratio).mean().detach()
            ratio_min = var.ratio.min().detach()
            ratio_max = var.ratio.max().detach()
            # log.debug('Learner %d ratio mean min max %.4f %.4f %.4f', self.policy_id, ratio_mean.cpu().item(), ratio_min.cpu().item(), ratio_max.cpu().item())

            value_delta = torch.abs(var.values - var.old_values)
            value_delta_avg, value_delta_max = value_delta.mean(), value_delta.max()

            # calculate KL-divergence with the behaviour policy action distribution
            old_action_distribution = get_action_distribution(
                self.actor_critic.action_space, var.mb.action_logits,
            )
            kl_old = var.action_distribution.kl_divergence(old_action_distribution)
            kl_old_mean = kl_old.mean()

            stats.kl_divergence = kl_old_mean
            stats.value_delta = value_delta_avg
            stats.value_delta_max = value_delta_max
            stats.fraction_clipped = ((var.ratio < var.clip_ratio_low).float() + (var.ratio > var.clip_ratio_high).float()).mean()
            stats.ratio_mean = ratio_mean
            stats.ratio_min = ratio_min
            stats.ratio_max = ratio_max
            stats.num_sgd_steps = var.num_sgd_steps

        # this caused numerical issues on some versions of PyTorch with second moment reaching infinity
        adam_max_second_moment = 0.0
        for key, tensor_state in self.optimizer.state.items():
            adam_max_second_moment = max(tensor_state['exp_avg_sq'].max().item(), adam_max_second_moment)
        stats.adam_max_second_moment = adam_max_second_moment

        version_diff = var.curr_policy_version - var.mb.policy_version
        stats.version_diff_avg = version_diff.mean()
        stats.version_diff_min = version_diff.min()
        stats.version_diff_max = version_diff.max()

        for key, value in stats.items():
            stats[key] = to_scalar(value)

        return stats

    def _update_pbt(self):
        """To be called from the training loop, same thread that updates the model!"""
        with self.pbt_mutex:
            if self.load_policy_id is not None:
                assert self.cfg.with_pbt

                log.debug('Learner %d loads policy from %d', self.policy_id, self.load_policy_id)
                self.load_from_checkpoint(self.load_policy_id)
                self.load_policy_id = None

            if self.new_cfg is not None:
                for key, value in self.new_cfg.items():
                    if self.cfg[key] != value:
                        log.debug('Learner %d replacing cfg parameter %r with new value %r', self.policy_id, key, value)
                        self.cfg[key] = value

                for param_group in self.optimizer.param_groups:
                    param_group['lr'] = self.cfg.learning_rate
                    param_group['betas'] = (self.cfg.adam_beta1, self.cfg.adam_beta2)
                    log.debug('Updated optimizer lr to value %.7f, betas: %r', param_group['lr'], param_group['betas'])

                self.new_cfg = None

    @staticmethod
    def load_checkpoint(checkpoints, device):
        if len(checkpoints) <= 0:
            log.warning('No checkpoints found')
            return None
        else:
            latest_checkpoint = checkpoints[-1]

            # extra safety mechanism to recover from spurious filesystem errors
            num_attempts = 3
            for attempt in range(num_attempts):
                try:
                    log.warning('Loading state from checkpoint %s...', latest_checkpoint)
                    checkpoint_dict = torch.load(latest_checkpoint, map_location=device)
                    return checkpoint_dict
                except Exception:
                    log.exception(f'Could not load from checkpoint, attempt {attempt}')

    def _load_state(self, checkpoint_dict, load_progress=True):
        if load_progress:
            self.train_step = checkpoint_dict['train_step']
            self.env_steps = checkpoint_dict['env_steps']
        self.actor_critic.load_state_dict(checkpoint_dict['model'])
        self.optimizer.load_state_dict(checkpoint_dict['optimizer'])
        log.info('Loaded experiment state at training iteration %d, env step %d', self.train_step, self.env_steps)

    def init_model(self, timing):
        self.actor_critic = create_actor_critic(self.cfg, self.obs_space, self.action_space, timing)
        self.actor_critic.model_to_device(self.device)
        self.actor_critic.share_memory()

    def load_from_checkpoint(self, policy_id):
        checkpoints = self.get_checkpoints(self.checkpoint_dir(self.cfg, policy_id))
        checkpoint_dict = self.load_checkpoint(checkpoints, self.device)
        if checkpoint_dict is None:
            log.debug('Did not load from checkpoint, starting from scratch!')
        else:
            log.debug('Loading model from checkpoint')

            # if we're replacing our policy with another policy (under PBT), let's not reload the env_steps
            load_progress = policy_id == self.policy_id
            self._load_state(checkpoint_dict, load_progress=load_progress)

    def initialize(self, timing):
        with timing.timeit('init'):
            # initialize the Torch modules
            if self.cfg.seed is None:
                log.info('Starting seed is not provided')
            else:
                log.info('Setting fixed seed %d', self.cfg.seed)
                torch.manual_seed(self.cfg.seed)
                np.random.seed(self.cfg.seed)

            # this does not help with a single experiment
            # but seems to do better when we're running more than one experiment in parallel
            torch.set_num_threads(1)

            if self.cfg.device == 'gpu':
                torch.backends.cudnn.benchmark = True

                # we should already see only one CUDA device, because of env vars
                assert torch.cuda.device_count() == 1
                self.device = torch.device('cuda', index=0)
            else:
                self.device = torch.device('cpu')
            self.init_model(timing)

            self.optimizer = torch.optim.Adam(
                self.actor_critic.parameters(),
                self.cfg.learning_rate,
                betas=(self.cfg.adam_beta1, self.cfg.adam_beta2),
                eps=self.cfg.adam_eps,
            )

            self.load_from_checkpoint(self.policy_id)

            self._broadcast_model_weights()  # sync the very first version of the weights

        self.train_thread_initialized.set()

    def _process_training_data(self, data, timing, wait_stats=None):
        self.is_training = True

        buffer, batch_size, samples, env_steps = data
        assert samples == batch_size * self.cfg.num_batches_per_iteration

        self.env_steps += env_steps
        experience_size = buffer.rewards.shape[0]

        stats = dict(learner_env_steps=self.env_steps, policy_id=self.policy_id)

        with timing.add_time('train'):
            discarding_rate = self._discarding_rate()

            self._update_pbt()

            train_stats = self._train(buffer, batch_size, experience_size, timing)

            if train_stats is not None:
                stats['train'] = train_stats

                if wait_stats is not None:
                    wait_avg, wait_min, wait_max = wait_stats
                    stats['train']['wait_avg'] = wait_avg
                    stats['train']['wait_min'] = wait_min
                    stats['train']['wait_max'] = wait_max

                stats['train']['discarded_rollouts'] = self.num_discarded_rollouts
                stats['train']['discarding_rate'] = discarding_rate

                stats['stats'] = memory_stats('learner', self.device)

        self.is_training = False

        try:
            self.report_queue.put(stats)
        except Full:
            log.warning('Could not report training stats, the report queue is full!')

    def _train_loop(self):
        timing = Timing()
        self.initialize(timing)

        wait_times = deque([], maxlen=self.cfg.num_workers)
        last_cache_cleanup = time.time()
        num_batches_processed = 0

        while not self.terminate:
            with timing.timeit('train_wait'):
                data = safe_get(self.experience_buffer_queue)

            if self.terminate:
                break

            wait_stats = None
            wait_times.append(timing.train_wait)

            if len(wait_times) >= wait_times.maxlen:
                wait_times_arr = np.asarray(wait_times)
                wait_avg = np.mean(wait_times_arr)
                wait_min, wait_max = wait_times_arr.min(), wait_times_arr.max()
                # log.debug(
                #     'Training thread had to wait %.5f s for the new experience buffer (avg %.5f)',
                #     timing.train_wait, wait_avg,
                # )
                wait_stats = (wait_avg, wait_min, wait_max)

            self._process_training_data(data, timing, wait_stats)
            num_batches_processed += 1

            if time.time() - last_cache_cleanup > 300.0 or (not self.cfg.benchmark and num_batches_processed < 50):
                if self.cfg.device == 'gpu':
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
                last_cache_cleanup = time.time()

        time.sleep(0.3)
        log.info('Train loop timing: %s', timing)
        del self.actor_critic
        del self.device

    def _experience_collection_rate_stats(self):
        now = time.time()
        if now - self.discarded_experience_timer > 1.0:
            self.discarded_experience_timer = now
            self.discarded_experience_over_time.append((now, self.num_discarded_rollouts))

    def _discarding_rate(self):
        if len(self.discarded_experience_over_time) <= 1:
            return 0

        first, last = self.discarded_experience_over_time[0], self.discarded_experience_over_time[-1]
        delta_rollouts = last[1] - first[1]
        delta_time = last[0] - first[0]
        discarding_rate = delta_rollouts / (delta_time + EPS)
        return discarding_rate

    def _extract_rollouts(self, data):
        data = AttrDict(data)
        worker_idx, split_idx, traj_buffer_idx = data.worker_idx, data.split_idx, data.traj_buffer_idx

        rollouts = []
        for rollout_data in data.rollouts:
            env_idx, agent_idx = rollout_data['env_idx'], rollout_data['agent_idx']
            tensors = self.rollout_tensors.index((worker_idx, split_idx, env_idx, agent_idx, traj_buffer_idx))

            rollout_data['t'] = tensors
            rollout_data['worker_idx'] = worker_idx
            rollout_data['split_idx'] = split_idx
            rollout_data['traj_buffer_idx'] = traj_buffer_idx
            rollouts.append(AttrDict(rollout_data))

        return rollouts

    def _process_pbt_task(self, pbt_task):
        task_type, data = pbt_task

        with self.pbt_mutex:
            if task_type == PbtTask.SAVE_MODEL:
                policy_id = data
                assert policy_id == self.policy_id
                self.should_save_model = True
            elif task_type == PbtTask.LOAD_MODEL:
                policy_id, new_policy_id = data
                assert policy_id == self.policy_id
                assert new_policy_id is not None
                self.load_policy_id = new_policy_id
            elif task_type == PbtTask.UPDATE_CFG:
                policy_id, new_cfg = data
                assert policy_id == self.policy_id
                self.new_cfg = new_cfg

    def _accumulated_too_much_experience(self, rollouts):
        max_minibatches_to_accumulate = self.cfg.num_minibatches_to_accumulate
        if max_minibatches_to_accumulate == -1:
            # default value
            max_minibatches_to_accumulate = 2 * self.cfg.num_batches_per_iteration

        # allow the max batches to accumulate, plus the minibatches we're currently training on
        max_minibatches_on_learner = max_minibatches_to_accumulate + self.cfg.num_batches_per_iteration

        minibatches_currently_training = int(self.is_training) * self.cfg.num_batches_per_iteration

        rollouts_per_minibatch = self.cfg.batch_size / self.cfg.rollout

        # count contribution from unprocessed rollouts
        minibatches_currently_accumulated = len(rollouts) / rollouts_per_minibatch

        # count minibatches ready for training
        minibatches_currently_accumulated += self.experience_buffer_queue.qsize() * self.cfg.num_batches_per_iteration

        total_minibatches_on_learner = minibatches_currently_training + minibatches_currently_accumulated

        return total_minibatches_on_learner >= max_minibatches_on_learner

    def _run(self):
        # workers should ignore Ctrl+C because the termination is handled in the event loop by a special msg
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        try:
            psutil.Process().nice(self.cfg.default_niceness)
        except psutil.AccessDenied:
            log.error('Low niceness requires sudo!')

        if self.cfg.device == 'gpu':
            cuda_envvars(self.policy_id)

        torch.multiprocessing.set_sharing_strategy('file_system')
        torch.set_num_threads(self.cfg.learner_main_loop_num_cores)

        timing = Timing()

        rollouts = []

        if self.train_in_background:
            self.training_thread.start()
        else:
            self.initialize(timing)
            log.error(
                'train_in_background set to False on learner %d! This is slow, use only for testing!', self.policy_id,
            )

        while not self.terminate:
            while True:
                try:
                    tasks = self.task_queue.get_many(timeout=0.005)

                    for task_type, data in tasks:
                        if task_type == TaskType.TRAIN:
                            with timing.add_time('extract'):
                                rollouts.extend(self._extract_rollouts(data))
                                # log.debug('Learner %d has %d rollouts', self.policy_id, len(rollouts))
                        elif task_type == TaskType.INIT:
                            self._init()
                        elif task_type == TaskType.TERMINATE:
                            time.sleep(0.3)
                            log.info('GPU learner timing: %s', timing)
                            self._terminate()
                            break
                        elif task_type == TaskType.PBT:
                            self._process_pbt_task(data)
                except Empty:
                    break

            if self._accumulated_too_much_experience(rollouts):
                # if we accumulated too much experience, signal the policy workers to stop experience collection
                if not self.stop_experience_collection[self.policy_id]:
                    self.stop_experience_collection_num_msgs += 1
                    # TODO: add a logger function for this
                    if self.stop_experience_collection_num_msgs >= 50:
                        log.info(
                            'Learner %d accumulated too much experience, stop experience collection! '
                            'Learner is likely a bottleneck in your experiment (%d times)',
                            self.policy_id, self.stop_experience_collection_num_msgs,
                        )
                        self.stop_experience_collection_num_msgs = 0

                self.stop_experience_collection[self.policy_id] = True
            elif self.stop_experience_collection[self.policy_id]:
                # otherwise, resume the experience collection if it was stopped
                self.stop_experience_collection[self.policy_id] = False
                with self.resume_experience_collection_cv:
                    self.resume_experience_collection_num_msgs += 1
                    if self.resume_experience_collection_num_msgs >= 50:
                        log.debug('Learner %d is resuming experience collection!', self.policy_id)
                        self.resume_experience_collection_num_msgs = 0
                    self.resume_experience_collection_cv.notify_all()

            with torch.no_grad():
                rollouts = self._process_rollouts(rollouts, timing)

            if not self.train_in_background:
                while not self.experience_buffer_queue.empty():
                    training_data = self.experience_buffer_queue.get()
                    self._process_training_data(training_data, timing)

            self._experience_collection_rate_stats()

        if self.train_in_background:
            self.experience_buffer_queue.put(None)
            self.training_thread.join()

    def init(self):
        self.task_queue.put((TaskType.INIT, None))
        self.initialized_event.wait()

    def save_model(self, timeout=None):
        self.model_saved_event.clear()
        save_task = (PbtTask.SAVE_MODEL, self.policy_id)
        self.task_queue.put((TaskType.PBT, save_task))
        log.debug('Wait while learner %d saves the model...', self.policy_id)
        if self.model_saved_event.wait(timeout=timeout):
            log.debug('Learner %d saved the model!', self.policy_id)
        else:
            log.warning('Model saving request timed out!')
        self.model_saved_event.clear()

    def close(self):
        self.task_queue.put((TaskType.TERMINATE, None))

    def join(self):
        join_or_kill(self.process)
