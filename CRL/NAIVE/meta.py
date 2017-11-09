import random
import torch
import copy
import numpy as np
import torch.optim as optim
from torch.autograd import Variable
import torch.nn.functional as F
from collections import namedtuple
from collections import deque

class MetaAgent(object):
	'''
	(1) act: how to sample an action to examine the learning 
		outcomes or explore the environment;
	(2) memorize: how to store observed observations in order to
		help learing or establishing the empirical model of the 
		enviroment;
	(3) learn: how the agent learns from the observations via 
		explicitor implicit inference, how to optimize the policy 
		model.
	'''
	def __init__(self, model, args, is_train=False):
		self.model         = model
		self.is_train      = is_train
		self.gamma         =   args.gamma
		self.epsilon       = args.epsilon
		self.epsilon_decay = args.epsilon_decay
		self.epsilon_delta = (args.epsilon - 0.05) / args.episode_num

		self.mem_size     = args.mem_size
		self.batch_size   = args.batch_size
		self.weight_num   = args.weight_num
		self.trans_mem    = deque()
		self.trans        = namedtuple('trans', ['s', 'a', 's_', 'r', 'd'])
		self.priority_mem = deque()

		if args.optimizer == 'Adam':
			self.optimizer = optim.Adam(self.model.parameters(), lr=args.lr)
		elif args.optimizer == 'RMSprop':
			self.optimizer = optim.RMSprop(self.model.parameters(), lr=args.lr)

		self.keep_preference = None
		# self.update_count = 0
		# self.update_freq  = args.update_freq

		if self.is_train: self.model.train()
		
		
	def act(self, state, preference=None):
		# random pick a preference if it is not specified
		if preference is None:
			if self.keep_preference is None:
				# preference = torch.from_numpy(
				# 	np.random.dirichlet(np.ones(self.model.reward_size))).float()
				self.keep_preference = torch.randn(self.model.reward_size)
				self.keep_preference = torch.abs(self.keep_preference) / \
									   torch.norm(self.keep_preference, p=1)
			preference = self.keep_preference
			# preference = random.choice(
			# 	[torch.FloatTensor([0.8,0.2]), torch.FloatTensor([0.2,0.8])])
		state = torch.from_numpy(state).float()

		_, Q = self.model(
				Variable(state.unsqueeze(0)), 
				Variable(preference.unsqueeze(0)))
		
		action = Q.max(1)[1].data.numpy()
		action = action[0]

		if self.is_train and (len(self.trans_mem) < self.batch_size or \
					torch.rand(1)[0] < self.epsilon):
				action = np.random.choice(self.model.action_size, 1)[0]
			
		return action


	def memorize(self, state, action, next_state, reward, terminal):
		self.trans_mem.append(self.trans(
							torch.from_numpy(state).float(),		# state
							action, 								# action
							torch.from_numpy(next_state).float(), 	# next state
							torch.from_numpy(reward).float(), 		# reward
							terminal))								# terminal
		
		# randomly produce a preference for calculating priority
		# preference = self.keep_preference
		preference = torch.randn(self.model.reward_size)
		preference = torch.abs(preference) / torch.norm(preference, p=1)
		state = torch.from_numpy(state).float()
		
		_, q  = self.model(Variable(state.unsqueeze(0), volatile=True),
						  Variable(preference.unsqueeze(0), volatile=True))
		q = q[0, action].data[0]
		wr = preference.dot(torch.from_numpy(reward).float())
		if not terminal:
			next_state = torch.from_numpy(next_state).float()
			hq, _ = self.model(Variable(next_state.unsqueeze(0), volatile=True),
						  Variable(preference.unsqueeze(0), volatile=True))
			hq = hq.data[0]
			p = abs(wr + self.gamma * hq - q)
		else:
			self.keep_preference = None
			if self.epsilon_decay:
				self.epsilon -= self.epsilon_delta
			p = abs(wr - q)
		p += 1e-5

		self.priority_mem.append(
				p
			)
		if len(self.trans_mem) > self.mem_size:
			self.trans_mem.popleft()
			self.priority_mem.popleft()


	def sample(self, pop, pri, k):
		pri = np.array(pri).astype(np.float)
		inds = np.random.choice(
				range(len(pop)), k,
				replace=False,
				p=pri/pri.sum()
			)
		return [pop[i] for i in inds]
	

	def actmsk(self, num_dim, index):
		mask = torch.ByteTensor(num_dim).zero_()
		mask[index] = 1
		return mask.unsqueeze(0)


	def nontmlinds(self, terminal_batch):
		mask = torch.ByteTensor(terminal_batch)
		inds = torch.arange(0, len(terminal_batch)).long()
		inds = inds[mask.eq(0)]
		return inds


	def learn(self):
		if len(self.trans_mem) > self.batch_size:
			minibatch = self.sample(self.trans_mem, self.priority_mem, self.batch_size)
			# minibatch = random.sample(self.trans_mem, self.batch_size)
			batchify = lambda x: list(x) * self.weight_num
			state_batch      = batchify(map(lambda x: x.s.unsqueeze(0), minibatch))
			action_batch     = batchify(map(lambda x: self.actmsk(self.model.action_size, x.a), minibatch))
			reward_batch     = batchify(map(lambda x: x.r.unsqueeze(0), minibatch))
			next_state_batch = batchify(map(lambda x: x.s_.unsqueeze(0), minibatch))
			terminal_batch   = batchify(map(lambda x: x.d, minibatch))

			preference_batch = np.random.randn(self.weight_num, self.model.reward_size)
			# # preference_batch = np.array([[0.8,0.2], [0.2, 0.8]])
			preference_batch = np.abs(preference_batch) / \
								np.linalg.norm(preference_batch, ord=1, axis=1, keepdims=True)
			# preference_batch = np.random.dirichlet(np.ones(self.model.reward_size), size=self.weight_num)								
			preference_batch = torch.from_numpy(preference_batch.repeat(self.batch_size, axis=0)).float()
			
			__, Q    = self.model(Variable(torch.cat(state_batch, dim=0)),
								  Variable(preference_batch))
			# detach since we don't want gradients to propagate
			HQ, _    = self.model(Variable(torch.cat(next_state_batch, dim=0)),
								  Variable(preference_batch))

			w_reward_batch = torch.bmm(preference_batch.unsqueeze(1),
									   	torch.cat(reward_batch, dim=0).unsqueeze(2)
									  ).squeeze()

			
			nontmlmask = self.nontmlinds(terminal_batch)
			Estimate_Q = Variable(torch.zeros(self.batch_size*self.weight_num))
			Estimate_Q[nontmlmask] = self.gamma * HQ[nontmlmask]
			Estimate_Q += Variable(w_reward_batch)

			self.optimizer.zero_grad()
			action_mask = Variable(torch.cat(action_batch, dim=0))
			loss = torch.sum((Q.masked_select(action_mask) - Estimate_Q).pow(2))			
			report_loss = loss.data[0]/(self.batch_size*self.weight_num)
			loss.backward()
			self.optimizer.step()

			return	report_loss
		
		return 1.0

	def reset(self):
		self.keep_preference = None
		if self.epsilon_decay:
				self.epsilon -= self.epsilon_delta


	def save(self, save_path, model_name):
		torch.save(self.model, "{}{}.pkl".format(save_path, model_name))


