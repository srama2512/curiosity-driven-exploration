import torch
import torch.nn as nn
import torch.nn.functional as F

import pdb
from distributions import Categorical, DiagGaussian
from utils import init, init_normc_


class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)


class Policy(nn.Module):
    def __init__(self, obs_shape, action_space, base_kwargs=None):
        super(Policy, self).__init__()
        if base_kwargs is None:
            base_kwargs = {}

        if len(obs_shape) == 3:
            self.base = CNNBase(obs_shape[0], **base_kwargs)
        elif len(obs_shape) == 1:
            self.base = MLPBase(obs_shape[0], **base_kwargs)
        else:
            raise NotImplementedError

        if action_space.__class__.__name__ == "Discrete":
            self.n_actions = action_space.n
            num_outputs = action_space.n
            self.dist = Categorical(self.base.output_size, num_outputs)
        elif action_space.__class__.__name__ == "Box":
            num_outputs = action_space.shape[0]
            self.dist = DiagGaussian(self.base.output_size, num_outputs)
        else:
            raise NotImplementedError

    @property
    def is_recurrent(self):
        return self.base.is_recurrent

    @property
    def recurrent_hidden_state_size(self):
        """Size of rnn_hx."""
        return self.base.recurrent_hidden_state_size

    def forward(self, inputs, rnn_hxs, masks):
        raise NotImplementedError

    def act(self, inputs, rnn_hxs, masks, deterministic=False):
        value, actor_features, rnn_hxs = self.base(inputs, rnn_hxs, masks)
        dist = self.dist(actor_features)

        if deterministic:
            action = dist.mode()
        else:
            action = dist.sample()

        action_log_probs = dist.log_probs(action)
        dist_entropy = dist.entropy().mean()

        return value, action, action_log_probs, rnn_hxs

    def act_curiosity(self, inputs, rnn_hxs, masks, deterministic=False):
        value, actor_features, rnn_hxs = self.base(inputs, rnn_hxs, masks)
        dist = self.dist(actor_features)

        if deterministic:
            action = dist.mode()
        else:
            action = dist.sample()

        action_log_probs = dist.log_probs(action)
        dist_entropy = dist.entropy().mean()

        return value, action, action_log_probs, rnn_hxs, actor_features

    def get_features(self, inputs, rnn_hxs, masks):
        _, actor_features, _ = self.base(inputs, rnn_hxs, masks)
        return actor_features

    def get_value(self, inputs, rnn_hxs, masks):
        value, _, _ = self.base(inputs, rnn_hxs, masks)
        return value

    def evaluate_actions(self, inputs, rnn_hxs, masks, action):
        value, actor_features, rnn_hxs = self.base(inputs, rnn_hxs, masks)
        dist = self.dist(actor_features)

        action_log_probs = dist.log_probs(action)
        dist_entropy = dist.entropy().mean()

        return value, action_log_probs, dist_entropy, rnn_hxs

    def evaluate_actions_curiosity(self, inputs, rnn_hxs, masks, action):
        value, actor_features, rnn_hxs = self.base(inputs, rnn_hxs, masks)
        dist = self.dist(actor_features)

        action_log_probs = dist.log_probs(action)
        dist_entropy = dist.entropy().mean()

        return value, action_log_probs, dist_entropy, rnn_hxs, actor_features

class NNBase(nn.Module):

    def __init__(self, recurrent, recurrent_input_size, hidden_size):
        super(NNBase, self).__init__()

        self._hidden_size = hidden_size
        self._recurrent = recurrent

        if recurrent:
            self.gru = nn.GRUCell(recurrent_input_size, hidden_size)
            nn.init.orthogonal_(self.gru.weight_ih.data)
            nn.init.orthogonal_(self.gru.weight_hh.data)
            self.gru.bias_ih.data.fill_(0)
            self.gru.bias_hh.data.fill_(0)

    @property
    def is_recurrent(self):
        return self._recurrent

    @property
    def recurrent_hidden_state_size(self):
        if self._recurrent:
            return self._hidden_size
        return 1

    @property
    def output_size(self):
        return self._hidden_size

    def _forward_gru(self, x, hxs, masks):
        if x.size(0) == hxs.size(0):
            x = hxs = self.gru(x, hxs * masks)
        else:
            # x is a (T, N, -1) tensor that has been flatten to (T * N, -1)
            N = hxs.size(0)
            T = int(x.size(0) / N)

            # unflatten
            x = x.view(T, N, x.size(1))

            # Same deal with masks
            masks = masks.view(T, N, 1)

            outputs = []
            for i in range(T):
                hx = hxs = self.gru(x[i], hxs * masks[i])
                outputs.append(hx)

            # assert len(outputs) == T
            # x is a (T, N, -1) tensor
            x = torch.stack(outputs, dim=0)
            # flatten
            x = x.view(T * N, -1)

        return x, hxs


class CNNBase(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=512, obs_mean=0.0, obs_std=255.0):
        super(CNNBase, self).__init__(recurrent, hidden_size, hidden_size)

        init_ = lambda m: init(m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain('relu'))
        self.obs_mean = obs_mean
        self.obs_std = obs_std

        self.main = nn.Sequential(
            init_(nn.Conv2d(num_inputs, 32, 8, stride=4)),
            nn.LeakyReLU(0.2, inplace=True),
            nn.BatchNorm2d(32),
            init_(nn.Conv2d(32, 64, 4, stride=2)),
            nn.LeakyReLU(0.2, inplace=True),
            nn.BatchNorm2d(64),
            init_(nn.Conv2d(64, 64, 3, stride=1)),
            nn.LeakyReLU(0.2, inplace=True),
            nn.BatchNorm2d(64),
            Flatten(),
            init_(nn.Linear(64 * 7 * 7, hidden_size)),
            nn.BatchNorm1d(hidden_size)
        )

        init_ = lambda m: init(m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0))

        self.critic_linear = init_(nn.Linear(hidden_size, 1))

        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = self.main((inputs - self.obs_mean)/(self.obs_std+1e-5))

        if self.is_recurrent:
            x, rnn_hxs = self._forward_gru(x, rnn_hxs, masks)

        return self.critic_linear(x), x, rnn_hxs


class MLPBase(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=64):
        super(MLPBase, self).__init__(recurrent, num_inputs, hidden_size)

        if recurrent:
            num_inputs = hidden_size

        init_ = lambda m: init(m,
            init_normc_,
            lambda x: nn.init.constant_(x, 0))

        self.actor = nn.Sequential(
            init_(nn.Linear(num_inputs, hidden_size)),
            nn.Tanh(),
            init_(nn.Linear(hidden_size, hidden_size)),
            nn.Tanh()
        )

        self.critic = nn.Sequential(
            init_(nn.Linear(num_inputs, hidden_size)),
            nn.Tanh(),
            init_(nn.Linear(hidden_size, hidden_size)),
            nn.Tanh()
        )

        self.critic_linear = init_(nn.Linear(hidden_size, 1))

        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = inputs

        if self.is_recurrent:
            x, rnn_hxs = self._forward_gru(x, rnn_hxs, masks)

        hidden_critic = self.critic(x)
        hidden_actor = self.actor(x)

        return self.critic_linear(hidden_critic), hidden_actor, rnn_hxs

class ForwardModel(nn.Module):
    """
    Given s_{t} encoding and a_{t}, it predicts s_{t+1} encoding
    """
    def __init__(self, n_actions, state_size=512, hidden_size=512):
        super(ForwardModel, self).__init__()

        init_ = lambda m: init(m,
            init_normc_,
            lambda x: nn.init.constant_(x, 0))

        self.pre_rb = nn.Sequential(
                        init_(nn.Linear(state_size + n_actions, hidden_size)),
                        nn.LeakyReLU(0.2, inplace=True),
                    )
        self.post_rb = init_(nn.Linear(hidden_size, state_size))

        class ResidualBlock(nn.Module):
            def __init__(self):
                super().__init__()
                self.fc1 = nn.Sequential(
                                init_(nn.Linear(hidden_size + n_actions, hidden_size)),
                                nn.LeakyReLU(0.2, inplace=True),
                           )
                self.fc2 = nn.Sequential(
                                init_(nn.Linear(hidden_size + n_actions, hidden_size))
                           )
            def forward(self, feat, act):
                x = feat
                x = self.fc1(torch.cat([x, act], dim=1))
                x = self.fc2(torch.cat([x, act], dim=1))
                return feat + x

        self.rb1 = ResidualBlock()
        self.rb2 = ResidualBlock()
        self.rb3 = ResidualBlock()
        self.rb4 = ResidualBlock()

    def forward(self, s, a):
        # s - batch_size x state_size
        # a - batch_size x n_actions (one-hot encoding)
        x = self.pre_rb(torch.cat([s, a], dim=1))
        x = self.rb1(x, a); x = self.rb2(x, a); x = self.rb3(x, a); x = self.rb4(x, a)
        sp = self.post_rb(x)
        return sp

class InverseModel(nn.Module):
    """
    Given s_{t}, s_{t+1} encoding, it predicts a_{t}
    """
    def __init__(self, n_actions, state_size=512, hidden_size=256):
        super(InverseModel, self).__init__()

        init_ = lambda m: init(m,
            init_normc_,
            lambda x: nn.init.constant_(x, 0))

        self.main = nn.Sequential(
                        init_(nn.Linear(2*state_size, hidden_size)),
                        nn.ReLU(inplace=True),
                        init_(nn.Linear(hidden_size, n_actions))
                    )

    def forward(self, s, sp):
        # s, sp - batch_size x state_size
        return self.main(torch.cat([s, sp], dim=1))
