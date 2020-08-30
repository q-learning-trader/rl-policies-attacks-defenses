from advertorch.attacks.base import Attack
import gym
import time
import torch
import torch.nn as nn
import warnings
import numpy as np
from typing import Any, Dict, List, Union, Optional, Callable

from tianshou.policy import BasePolicy
from tianshou.data import Batch, to_numpy


class strategically_timed_attack_collector:
    """
    :param policy: an instance of the :class:`~tianshou.policy.BasePolicy`
        class.
    :param env: a ``gym.Env`` environment or an instance of the
        :class:`~tianshou.env.BaseVectorEnv` class.
    :param obs_adv_atk: an instance of the :class:`~advertorch.attacks.base.Attack`
        class implementing an image adversarial attack.
    :param beta: attacks only if max(prob actions) - min(prob actions) >= beta
    :param softmax: if true, apply softmax to convert logits into probabilities
    :param perfect_attack: force adversarial attacks on observations to be
        always effective (ignore the ``adv`` param).
    """
    def __init__(self,
                 policy: BasePolicy,
                 env: gym.Env,
                 obs_adv_atk: Attack,
                 beta: float = 0.5,
                 softmax: bool = True,
                 perfect_attack: bool = False,
                 ):
        self.policy = policy
        self.env = env
        self.adv = obs_adv_atk  # advertorch attack method
        self.beta = beta
        assert 0 <= beta, \
            "beta should >= 0"
        self.softmax = softmax
        self.perfect_attack = perfect_attack
        self.action_space = self.env.action_space.shape or self.env.action_space.n
        self.data = Batch(state={}, obs={}, act={}, rew={}, done={}, info={},
                          obs_next={}, policy={})
        self.reset_env()

    def reset_env(self):
        self.data.obs = self.env.reset()

    def collect(self,
                n_step: int = 0,
                n_episode: int = 0,
                render: Optional[float] = None,
                device: str = 'cuda' if torch.cuda.is_available() else 'cpu'
                ) -> Dict[str, float]:

        """
        :return: A dict including the following keys
            * ``n/ep`` the collected number of episodes.
            * ``n/st`` the collected number of steps.
            * ``v/st`` the speed of steps per second.
            * ``v/ep`` the speed of episode per second.
            * ``rew`` the mean reward over collected episodes.
            * ``len`` the mean length over collected episodes.
            * ``n_attacks`` number of performed attacks.
            * ``n_succ_atks`` number of performed successful attacks.
            * ``n_attacks(%)`` ratio of performed attacks over steps.
            * ``succ_atks(%)`` ratio of successful attacks over performed attacks.
        """
        assert (n_step and not n_episode) or (not n_step and n_episode), \
            "One and only one collection number specification is permitted!"
        start_time = time.time()
        episode_count = 0
        reward_total = 0.0
        self.reset_env()

        frames_count = 0  # number of observed frames
        n_attacks = 0  # number of attacks performed
        succ_atk = 0  # number of successful image attacks

        while True:
            if frames_count >= 100000 and episode_count == 0:
                warnings.warn(
                    'There are already many steps in an episode. '
                    'You should add a time limitation to your environment!',
                    Warning)

            # calculate the next action
            with torch.no_grad():
                self.data.obs = np.expand_dims(self.data.obs, axis=0)
                result = self.policy(self.data, last_state=None)
            self.data.act = to_numpy(result.act)

            ##########ADVERSARIAL ATTACK#########
            if self.softmax:
                softmax = nn.Softmax(dim=1)
                prob_a = softmax(result.logits).cpu().detach().numpy()  # distribution over actions
            else:
                prob_a = result.logits.cpu().detach().numpy()
            max_a = np.amax(prob_a)
            min_a = np.amin(prob_a)
            diff = max_a - min_a
            if diff >= self.beta:
                des_act = np.argmin(prob_a)  # get the desired actions
                if not self.perfect_attack:
                    ori_obs = torch.FloatTensor(self.data.obs).to(device)  # get the original  observations
                    des_act_t = torch.tensor([des_act]).to(device)
                    adv_obs = self.adv.perturb(ori_obs, des_act_t)  # create adversarial observations
                    y = self.adv.predict(adv_obs)
                    _, adv_actions = torch.max(y, 1)  # predict adversarial actions
                    self.data.act = adv_actions.cpu().detach().numpy()  # replace original actions with adversarial actions
                else:
                    self.data.act = [des_act]
                if self.data.act == des_act:
                    succ_atk += 1
                n_attacks += 1
            frames_count += 1
            ####################################

            obs_next, rew, done, info = self.env.step(self.data.act[0])
            self.data.update(obs_next=obs_next, rew=rew, done=done, info=info)

            reward_total += rew

            if render:
                self.render()
                time.sleep(render)

            if self.data.done:
                episode_count += 1
                self.reset_env()

            self.data.obs = self.data.obs_next

            if n_step:
                if frames_count >= n_step:
                    break
            else:
                if episode_count >= n_episode:
                    break

        # generate the statistics
        duration = max(time.time() - start_time, 1e-9)
        # average reward across the number of episodes
        reward_avg = reward_total / episode_count

        return {
            'n/ep': episode_count,
            'n/st': frames_count,
            'v/st': frames_count / duration,
            'v/ep': episode_count / duration,
            'rew': reward_avg,
            'len': frames_count / episode_count,
            'n_atks': n_attacks / np.sum(n_episode),
            'n_succ_atks': succ_atk / np.sum(n_episode),
            'atk_rate(%)': n_attacks / frames_count,
            'succ_atks(%)': succ_atk / n_attacks if n_attacks > 0 else 0,
        }