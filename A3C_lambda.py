import torch
from torch import nn
import torch.nn.functional as F
import flappy_bird_gym
from flappy_bird_env_simple import FlappyBirdEnvSimple
import gym
import torch.multiprocessing as mp
import os
import pickle
import time


def t(m_array):
    """Converts a multidimensional array to a torch tensor."""
    return torch.Tensor(m_array)


class SharedAdam(torch.optim.Adam):
    """An Adam optimizer shared by all workers."""
    def __init__(self, params, lr=1e-4, betas=(0.9, 0.999), eps=1e-8,
            weight_decay=0):
        """Creates an Adam optimizer."""
        super(SharedAdam, self).__init__(params, lr=lr, betas=betas, eps=eps,
                weight_decay=weight_decay)
        # Comment
        for group in self.param_groups:
            for p in group['params']:
                state = self.state[p]
                state['step'] = 0
                state['exp_avg'] = torch.zeros_like(p.data)
                state['exp_avg_sq'] = torch.zeros_like(p.data)

                state['exp_avg'].share_memory_()
                state['exp_avg_sq'].share_memory_()


class Worker(mp.Process):
    """A single reinforcement agent worker."""
    def __init__(self, parameters, i):
        """Creates a worker."""
        super(Worker, self).__init__()

        # Environment, network, optimizer
        self.env = gym.make(parameters['environment name'])
        #self.env = FlappyBirdEnvSimple()
        self.local_actor_critic = ActorCritic([self.env.observation_space.shape[0]], self.env.action_space.n)
        self.global_actor_critic = parameters['global actor critic']
        self.optimizer = parameters['optimizer']

        # Constants
        self.max_episodes = parameters['max episodes']
        self.t_max = parameters['t_max']
        self.entropy_reg_factor = parameters['entropy regularization factor']
        self.gamma = parameters['gamma']

        # Memory
        self.memory = Memory()

        # Worker id and episode index
        self.episode_idx = parameters['global episode index']
        self.name = 'w%02i' % i

        # Initialize the local network
        self.local_actor_critic.load_state_dict(
            self.global_actor_critic.state_dict())

        # Initialize Data Storage
        self.data = DataStorage(parameters['rel_path'],
                                 parameters['environment name'],
                                 parameters['probplot frequency']
                                 )

    def run(self):
        """The tasks of a single worker.
        The workers interact with the environment here."""
        for i in range(1, self.max_episodes + 1): # For every episode
            # Reset the parameters and the environment
            done = False
            total_reward = 0
            observation = self.env.reset()
            steps = 1

            while not done:  # Until episode is done
                # Sample an action based on state
                state = torch.tensor([observation], dtype=torch.float)
                probs, value = self.local_actor_critic.forward(state)
                probs = torch.softmax(probs, dim=1)
                dist = torch.distributions.Categorical(probs)
                action = dist.sample().numpy()[0]

                # Simulate environment
                observation_, reward, done, info = self.env.step(action)

                total_reward += reward
                if total_reward > 95:
                    print(self.name, total_reward, info['score'])

                # Add data to memory
                self.memory.add(observation, action, reward, value)

                # Add probability data to the data storage object
                if i % self.data.prob_freq == 0:
                    self.data.add_prob(probs)

                if steps % self.t_max == 0 or done: # If episode is done or
                                                    # tmax is reached
                    # Add next state to state list
                    self.memory.states.append(observation_)
                    # Train locally
                    self.train(done)

                    # Update the global network and copy it to local network
                    self.update_global()

                    # Reset the memory
                    self.memory.clear()

                # Iterate to the next state
                steps += 1
                observation = observation_

            # Increment the episode index
            with self.episode_idx.get_lock():
                self.episode_idx.value += 1

            # Print the result
            print(self.name, 'episode', self.episode_idx.value, total_reward, 'probability', probs)

            # Add score and conv data to the data storage object
            self.data.add_score(total_reward)
            self.data.add_conv(self.local_actor_critic)

            if i % 10000 == 0: # For every 1 000 000 episodes
                # Save data in textfiles
                self.data.save_score(self.name)
                self.data.save_conv(self.name)

                # Clear data storage
                self.data.clear_data()

            # Save prob_plot
            if i % self.data.prob_freq == 0:
                self.data.save_prob(self.name, i)

        # ----- At the end of the session -----

        # Save data in textfiles
        self.data.save_score(self.name)
        self.data.save_conv(self.name)

        # Clear data storage
        self.data.clear_data()

        # Save the global network in a textfile when worker 00 terminates
        if self.name == 'w00':
            self.data.save_net(self.global_actor_critic)

    def train(self, done):
        """Train the neural networks (actor and critic) based on the
            recent memory."""
        # Convert states and actions to tensors
        states = torch.tensor(self.memory.states, dtype=torch.float)
        actions = torch.tensor(self.memory.actions, dtype=torch.float)

        # Calculate the action distributions and values
        probs, values = self.local_actor_critic.forward(states)

        # Approximate the value function of the last state and initial values for
        # the return and lambda return
        R = values[-1] * (1 - int(done))
        Rlamda = R

        # Hyper parameter
        lamda = 1

        # If last state, the value function is zero
        if done:
            values[-1] = 0

        # Construct return lists
        batch_return = []
        batch_return_lamda = []

        for i in range(len(self.memory.rewards) - 1, -1, -1): # For t-1 to 0
            # Calculate returns
            R = self.memory.rewards[i] + self.gamma * R
            Rlamda = self.memory.rewards[i] + self.gamma * (lamda * Rlamda + (1 - lamda) * values[i+1])
            # Append to return lists
            batch_return.append(R)
            batch_return_lamda.append(Rlamda)

        # Reverse the list of returns
        batch_return.reverse()
        batch_return_lamda.reverse()

        # Convert the lists of returns to a tensor
        batch_return = torch.tensor(batch_return, dtype=torch.float)
        returns = batch_return
        batch_return_lamda = torch.tensor(batch_return_lamda, dtype=torch.float)
        returns_lamda = batch_return_lamda

        # Calculate the critic loss with lambda returns
        values = values.squeeze()
        critic_loss = (returns_lamda-values[:-1])**2

        # Calculate the log probabilites
        probs = torch.softmax(probs, dim=1)
        dist = torch.distributions.Categorical(probs[:-1])
        log_probs = dist.log_prob(actions)

        # Calculate the actor loss
        actor_loss = -log_probs*(returns-values[:-1])

        # Calculate the entropy
        entropy = -torch.mul(probs, torch.log(probs)).mean()


        # Calculate the total loss
        total_loss = (critic_loss + actor_loss).mean() - entropy * self.entropy_reg_factor

        # Reset the gradient and propagate the loss
        self.optimizer.zero_grad()
        total_loss.backward()

    def update_global(self):
        """Update the global network based on the gradient of the worker."""
        # Transfer gradient
        for local_param, global_param in zip(self.local_actor_critic.parameters(),
                                             self.global_actor_critic.parameters()):
            global_param._grad = local_param.grad

        # Update the global network based on gradient
        self.optimizer.step()

        # Copy the global network to the local network
        self.local_actor_critic.load_state_dict(
            self.global_actor_critic.state_dict())


class Memory():
    """The memory of a single worker."""
    def __init__(self):
        """Creates attributes."""
        self.states = []
        self.actions = []
        self.rewards = []
        self.values = []

    def add(self, state, action, reward, value):
        """Add experience to the memory."""
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.values.append(value)

    def clear(self):
        """Clear the memory."""
        self.states = []
        self.actions = []
        self.rewards = []
        self.values = []


class DataStorage():
    """Store data not directly required for the algorithm to run."""
    """Non of the methods are required for the algorithm to function."""
    def __init__(self, path, env_name, prob_freq):

        # path for data storage
        self.path = path

        # Generates test states for the convergence plot dependent on the environment
        self.test_states = self._teststates(env_name)

        # Initialize lists for plot data
        self.score_plot = []
        self.conv_plot = []
        self.prob_plot = []

        # Frequency of storage of probability plot
        self.prob_freq = prob_freq

    def clear_data(self):
        """Clear the data. Does not clear prob_plot."""
        self.score_plot = []
        self.conv_plot = []

    def add_score(self, total_reward):
        """Add score data."""
        self.score_plot.append(total_reward)

    def add_conv(self, local_actor_critic):
        """Add convergence data."""
        temp_list = []
        for state in self.test_states:
            _, value = local_actor_critic(t(state))
            temp_list.append(value)
        self.conv_plot.append(torch.mean(t(temp_list)))

    def add_prob(self, probs):
        """Add probability data."""
        self.prob_plot.append(probs.squeeze()[0])

    def save_score(self, name):
        """Save the score data in a textfile."""
        folderpath = os.path.join(self.path, 'score_plot')
        filepath = os.path.join(folderpath, name + '.txt')
        f = open(filepath, 'a+')
        for element in self.score_plot:
            f.write(str(element) + '\n')
        f.close()

    def save_conv(self, name):
        """Save the convergence data in a textfile."""
        folderpath = os.path.join(self.path, 'conv_plot')
        filepath = os.path.join(folderpath, name + '.txt')
        f = open(filepath, 'a+')
        for element in self.conv_plot:
            f.write(str(element.item()) + '\n')
        f.close()

    def save_prob(self, name, episode_nr):
        """Save the probability data in a textfile and clear the prob_plot data."""
        folderpath = os.path.join(self.path, 'prob_plot', name)
        filepath = os.path.join(folderpath, 'episode ' + str(episode_nr) + '.txt')
        f = open(filepath, 'a+')
        for element in self.prob_plot:
            f.write(str(element.item()) + '\n')
        f.close()

        # Clear the data
        self.prob_plot = []

    def save_net(self, network):
        """Save the network in a textfile."""
        filepath = os.path.join(self.path, 'network.txt')
        f = open(filepath, 'wb')
        pickle.dump(network, f)
        f.close()

    def _teststates(self, env_name):
        """Generate test states for the convergence plot
        dependent on the environment (FlappyBird or CartPole)."""
        teststates = []
        if env_name == 'FlappyBird-v0':
            for i in range(0, 280, 112):
                for j in range(0, 510, 204):
                    #for k in range(-8, 11, 8):
                    teststates.append(t([i, j]))
        else:
            for i in range(-4, 5, 10):
                for j in range(-10, 11, 20):
                    for k in range(-418, 419, 836):
                        for l in range(-10, 11, 20):
                            teststates.append(t([i, j, k / float(1000), l]))
        return teststates


class ActorCritic(nn.Module):
    """A neural network which works as a function approximator
    for the policy and value function."""
    def __init__(self, state_dim, n_actions):
        """Create the neural network."""
        super().__init__()
        self.first = nn.Linear(*state_dim, 128)
        self.common_mid = nn.Linear(128, 128)
        self.critic_mid = nn.Linear(128, 64)
        self.actor_mid = nn.Linear(128, 64)
        self.critic = nn.Linear(64, 1)
        self.actor = nn.Linear(64, n_actions)

    def forward(self, state):
        """Sample an action distribution from the policy and the value
        function approximation based on the current state."""
        first = torch.tanh(self.first(state))
        common_mid = torch.tanh(self.common_mid(first))
        actor_mid = torch.tanh(self.actor_mid(common_mid))
        critic_mid = torch.tanh(self.critic_mid(common_mid))
        actor = self.actor(actor_mid)
        critic = self.critic(critic_mid)
        return actor, critic


def main(parameters, network=None):
    """Initialize relevant parameters and start the workers."""
    # Create a temporary environment
    temp_env = gym.make(parameters['environment name'])
    #temp_env = FlappyBirdEnvSimple()
    # Create a global network or loads a new one
    if network:
        parameters['global actor critic'] = network
    else:
        parameters['global actor critic'] = ActorCritic(
        [temp_env.observation_space.shape[0]], # Dimension of observation space
        temp_env.action_space.n # Dimension of action space
        )

    # Share the memory of the global network
    parameters['global actor critic'].share_memory()

    # Create a shared Adam optimizer
    parameters['optimizer'] = SharedAdam(
    parameters['global actor critic'].parameters(),
    lr=parameters['learning rate']
    )

    # Create global episode count
    parameters['global episode index'] = global_ep = mp.Value('i', 0)

    # Create workers
    workers = [Worker(parameters, i)
               for i in range(parameters['number of threads'])]
    # Start the workers
    [w.start() for w in workers]

    # Wait for workers to finish
    [w.join() for w in workers]
