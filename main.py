import copy
import glob
import os
import time

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from baselines.common.vec_env.dummy_vec_env import DummyVecEnv
from baselines.common.vec_env.subproc_vec_env import SubprocVecEnv
from baselines.common.vec_env.vec_normalize import VecNormalize
from envs import make_env
from model import Policy
from storage import RolloutStorage
import tensorflow as tf
import cv2
from scipy import ndimage

import utils

import algo

from arguments import get_args
args = get_args()

assert args.algo in ['a2c', 'ppo', 'acktr']
if args.recurrent_policy:
    assert args.algo in ['a2c', 'ppo'], \
        'Recurrent policy is not implemented for ACKTR'

torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)

print('######## SUMMARY OF LOGGING ########')
try:
    os.makedirs(args.save_dir)
    print('Dir empty, making new log dir...')
except Exception as e:
    if e.__class__.__name__ in ['FileExistsError']:
        print('Dir exsit, checking checkpoint...')
    else:
        raise e
print('Log to {}'.format(args.save_dir))
print('Summarize_behavior_interval: {} minutes'.format(args.summarize_behavior_interval))
print('Summarize_observation: {}'.format(args.summarize_observation))
print('Summarize_rendered_behavior: {}'.format(args.summarize_rendered_behavior))
print('Summarize_state_prediction: {}'.format(args.summarize_state_prediction))
print('####################################')

log_fourcc = cv2.VideoWriter_fourcc(*'MJPG')
log_fps = 10

torch.set_num_threads(1)

summary_writer = tf.summary.FileWriter(args.save_dir)

bottom_envs = [make_env(i, args=args)
            for i in range(args.num_processes)]

if args.num_processes > 1:
    bottom_envs = SubprocVecEnv(bottom_envs)
else:
    bottom_envs = bottom_envs[0]()

if 'Bullet' in args.env_name:
    # if len(bottom_envs.observation_space.shape) == 1:
    #     from envs import VecNormalize
    #     if args.gamma is None:
    #         bottom_envs = VecNormalize(bottom_envs, ret=False)
    #     else:
    #         bottom_envs = VecNormalize(bottom_envs, gamma=args.gamma)
    pass

elif (args.env_name in ['OverCooked','MineCraft','Explore2D','GridWorld']) or ('NoFrameskip-v4' in args.env_name):
    pass

else:
    raise NotImplemented

if args.env_name in ['MineCraft']:
    import minecraft
    minecraft.minecraft_global_setup()

# if len(bottom_envs.observation_space.shape) == 1:
#     if args.env_name in ['OverCooked']:
#         raise Exception("I donot know why they have VecNormalize for ram observation")
#     bottom_envs = VecNormalize(bottom_envs, gamma=args.gamma)

obs_shape = bottom_envs.observation_space.shape
obs_shape = (obs_shape[0] * args.num_stack, *obs_shape[1:])

if len(obs_shape)==3 and (obs_shape[1]==84) and (obs_shape[2]==84):
    '''standard image state of 84*84'''
    state_type = 'standard_image'
else:
    '''any thing else is treated as a one-dimentional vector'''
    state_type = 'vector'

if len(args.num_subpolicy) != (args.num_hierarchy-1):
    print('# WARNING: Exlicity num_subpolicy is not matching args.num_hierarchy, use the first num_subpolicy for all layers')
    args.num_subpolicy = [args.num_subpolicy[0]]*(args.num_hierarchy-1)
'''for top hierarchy layer'''
args.num_subpolicy += [2]

if len(args.transition_model_mini_batch_size) != (args.num_hierarchy-1):
    print('# WARNING: Exlicity transition_model_mini_batch_size is not matching args.num_hierarchy, use the first transition_model_mini_batch_size for all layers')
    args.transition_model_mini_batch_size = [args.transition_model_mini_batch_size[0]]*(args.num_hierarchy-1)

if len(args.hierarchy_interval) != (args.num_hierarchy-1):
    print('# WARNING: Exlicity hierarchy_interval is not matching args.num_hierarchy, use the first hierarchy_interval for all layers')
    args.hierarchy_interval = [args.hierarchy_interval[0]]*(args.num_hierarchy-1)

if len(args.num_steps) != (args.num_hierarchy):
    print('# WARNING: Exlicity num_steps is not matching args.num_hierarchy, use the first num_steps for all layers')
    args.num_steps = [args.num_steps[0]]*(args.num_hierarchy)

input_actions_onehot_global = []
for hierarchy_i in range(args.num_hierarchy):
    input_actions_onehot_global += [torch.zeros(args.num_processes, args.num_subpolicy[hierarchy_i]).cuda()]
'''init top layer input_actions'''
input_actions_onehot_global[-1][:,0]=1.0

def puton_input_action_text(img):
    font                   = cv2.FONT_HERSHEY_SIMPLEX
    bottomLeftCornerOfText = (10,40)
    fontScale              = 1
    fontColor              = (0,0,0)
    lineType               = 2
    cv2.putText(
        img,
        '{}'.format(
            input_actions_onehot_global[0][0].cpu().numpy().astype(np.int64),
        ),
        bottomLeftCornerOfText,
        font,
        fontScale,
        fontColor,
        lineType,
    )
    return img

def get_mass_center(obs):
    return np.asarray(
        ndimage.measurements.center_of_mass(
            (
                (obs+255.0)/2.0
            ).astype(np.uint8)
        )
    )

sess = tf.Session()

if args.env_name in ['Explore2D']:
    import tables
    import numpy as np

    from pathlib import Path

    my_file = Path('{}/terminal_states.h5'.format(
        args.save_dir,
    ))
    if not my_file.is_file():
        terminal_states_f = tables.open_file(
            '{}/terminal_states.h5'.format(
                args.save_dir,
            ),
            mode='w',
        )
        atom = tables.Float64Atom()
        array_c = terminal_states_f.create_earray(terminal_states_f.root, 'data', atom, (0, 2))

        x = np.zeros((1, 2))
        array_c.append(x)

        terminal_states_f.close()

def obs_to_state_img(obs, marker='o',c='blue'):
    if state_type in ['standard_image']:
        state_img = obs[0]
    elif state_type in ['vector']:
        if args.env_name in ['Explore2D']:
            state_img = np.zeros((args.episode_length_limit*2+1,args.episode_length_limit*2+1))
            state_img[
                np.clip(
                    int(obs[0,0,0]),
                    -args.episode_length_limit,
                    +args.episode_length_limit
                )+args.episode_length_limit,
                np.clip(
                    int(obs[0,0,1]),
                    -args.episode_length_limit,
                    +args.episode_length_limit
                )+args.episode_length_limit
            ] = 255
        elif ('Bullet' in args.env_name) or (args.env_name in ['Explore2DContinuous']):
            import matplotlib.pyplot as plt
            plt.clf()
            axes = plt.gca()
            if ('MinitaurBulletEnv' in args.env_name) or ('AntBulletEnv' in args.env_name):
                limit = 2.0
                plt.scatter(obs[28], obs[29], s=24, c=c, marker=marker, alpha=1.0)
            elif args.env_name in ['ReacherBulletEnv-v1']:
                limit = 1
                plt.scatter(obs[0], obs[1], s=18, c=c, marker=marker, alpha=1.0)
            elif args.env_name in ['Explore2DContinuous']:
                limit = args.episode_length_limit
                plt.scatter(obs[0], obs[1], s=18, c=c, marker=marker, alpha=1.0)
            else:
                raise NotImplemented
            axes.set_xlim([-limit,limit])
            axes.set_ylim([-limit,limit])
            from utils import figure_to_array
            state_img = figure_to_array(plt.gcf())
            state_img = cv2.cvtColor(state_img, cv2.cv2.COLOR_RGBA2RGB)
        else:
            raise NotImplemented
    else:
        raise NotImplemented
    state_img = state_img.astype(np.uint8)
    return state_img

if args.test_action:
     from visdom import Visdom
     viz = Visdom(port=6009)
     win = None
     win_dic = {}
     win_dic['Obs'] = None

if args.act_deterministically:
    print('==========================================================================')
    print("================ Note that I am acting deterministically =================")
    print('==========================================================================')

if args.distance in ['match']:
    sift = cv2.xfeatures2d.SIFT_create()
    FLANN_INDEX_KDTREE = 1
    index_params = dict(algorithm = FLANN_INDEX_KDTREE, trees = 5)
    search_params = dict(checks = 50)
    flann = cv2.FlannBasedMatcher(index_params, search_params)

if args.inverse_mask:
    from model import InverseMaskModel
    inverse_mask_model = InverseMaskModel(
        predicted_action_space = bottom_envs.action_space.n,
        num_grid = args.num_grid,
    ).cuda()
    try:
        inverse_mask_model.load_state_dict(torch.load(args.save_dir+'/inverse_mask_model.pth'))
        print('Load inverse_mask_model previous point: Successed')
    except Exception as e:
        print('Load inverse_mask_model previous point: Failed, due to {}')


    optimizer_inverse_mask_model = optim.Adam(inverse_mask_model.parameters(), lr=1e-4, betas=(0.0, 0.9))
    NLLLoss = nn.NLLLoss(reduction='elementwise_mean')

    def normalize_mask_np(mask):
        return (mask-np.amin(mask))/(np.amax(mask)-np.amin(mask))

    def normalize_mask_torch(mask):
        return (mask-mask.min())/(mask.max()-mask.min())

    def binarize_mask_torch(mask):
        return ((mask-mask.max()).sign()+1.0)

    def update_inverse_mask_model(bottom_layer):

        epoch_loss = {}
        inverse_mask_model.train()

        for e in range(4):

            data_generator = bottom_layer.rollouts.transition_model_feed_forward_generator(
                mini_batch_size = bottom_layer.args.actor_critic_mini_batch_size,
            )

            for sample in data_generator:

                observations_batch, next_observations_batch, action_onehot_batch, reward_bounty_raw_batch = sample

                optimizer_inverse_mask_model.zero_grad()

                '''forward'''
                inverse_mask_model.train()

                action_lable_batch = action_onehot_batch.nonzero()[:,1]

                '''compute loss_action'''
                predicted_action_log_probs, loss_ent, predicted_action_log_probs_each = inverse_mask_model(
                    last_states  = observations_batch[:,-1:],
                    now_states   = next_observations_batch,
                )
                loss_action = NLLLoss(predicted_action_log_probs, action_lable_batch)

                '''compute loss_action_each'''
                action_lable_batch_each = action_lable_batch.unsqueeze(1).expand(-1,predicted_action_log_probs_each.size()[1]).contiguous()
                loss_action_each = NLLLoss(
                    predicted_action_log_probs_each.view(predicted_action_log_probs_each.size()[0] * predicted_action_log_probs_each.size()[1],predicted_action_log_probs_each.size()[2]),
                    action_lable_batch_each        .view(action_lable_batch_each        .size()[0] * action_lable_batch_each        .size()[1]                                          ),
                ) * action_lable_batch_each.size()[1]

                '''compute loss_inverse_mask_model'''
                loss_inverse_mask_model = loss_action + 0.001*loss_ent + loss_action_each

                '''backward'''
                loss_inverse_mask_model.backward()

                optimizer_inverse_mask_model.step()

        epoch_loss['loss_action'] = loss_action.item()
        epoch_loss['loss_ent'] = loss_ent.item()
        epoch_loss['loss_action_each'] = loss_action_each.item()
        epoch_loss['loss_inverse_mask_model'] = loss_inverse_mask_model.item()

        return epoch_loss


class HierarchyLayer(object):
    """docstring for HierarchyLayer."""
    """
    HierarchyLayer is a learning system, containning actor_critic, agent, rollouts.
    In the meantime, it is a environment, which has step, reset functions, as well as action_space, observation_space, etc.
    """
    def __init__(self, envs, hierarchy_id):
        super(HierarchyLayer, self).__init__()

        self.envs = envs
        self.hierarchy_id = hierarchy_id
        self.args = args

        '''as an env, it should have action_space and observation space'''
        self.action_space = gym.spaces.Discrete((input_actions_onehot_global[self.hierarchy_id]).size()[1])
        self.observation_space = self.envs.observation_space
        if self.hierarchy_id not in [args.num_hierarchy-1]:
            self.hierarchy_interval = args.hierarchy_interval[self.hierarchy_id]
        else:
            self.hierarchy_interval = None

        print('[H-{:1}] Building hierarchy layer. Action space {}. Observation_space {}. Hierarchy interval {}'.format(
            self.hierarchy_id,
            self.action_space,
            self.observation_space,
            self.hierarchy_interval,
        ))

        self.actor_critic = Policy(
            obs_shape = obs_shape,
            state_type = state_type,
            input_action_space = self.action_space,
            output_action_space = self.envs.action_space,
            recurrent_policy = args.recurrent_policy,
            num_subpolicy = args.num_subpolicy[self.hierarchy_id],
        ).cuda()

        if args.reward_bounty > 0.0 and self.hierarchy_id not in [0]:
            from model import TransitionModel
            self.transition_model = TransitionModel(
                input_observation_shape = obs_shape if not args.mutual_information else self.envs.observation_space.shape,
                state_type = state_type,
                input_action_space = self.envs.action_space,
                output_observation_shape = self.envs.observation_space.shape,
                num_subpolicy = args.num_subpolicy[self.hierarchy_id-1],
                mutual_information = args.mutual_information,
            ).cuda()
            self.action_onehot_batch = torch.zeros(args.num_processes*self.envs.action_space.n,self.envs.action_space.n).cuda()
            batch_i = 0
            for action_i in range(self.envs.action_space.n):
                for process_i in range(args.num_processes):
                    self.action_onehot_batch[batch_i][action_i] = 1.0
                    batch_i += 1
        else:
            self.transition_model = None

        if args.algo == 'a2c':
            self.agent = algo.A2C_ACKTR(
                self.actor_critic, args.value_loss_coef, args.entropy_coef,
                lr=args.lr,
                eps=args.eps,
                alpha=args.alpha,
                max_grad_norm=args.max_grad_norm,
            )
        elif args.algo == 'ppo':
            self.agent = algo.PPO()
        elif args.algo == 'acktr':
            self.agent = algo.A2C_ACKTR(
                self.actor_critic, args.value_loss_coef, args.entropy_coef,
                acktr=True,
            )

        self.rollouts = RolloutStorage(
            num_steps = args.num_steps[self.hierarchy_id],
            num_processes = args.num_processes,
            obs_shape = obs_shape,
            input_actions = self.action_space,
            action_space = self.envs.action_space,
            state_size = self.actor_critic.state_size,
            observation_space = self.envs.observation_space,
        ).cuda()
        self.current_obs = torch.zeros(args.num_processes, *obs_shape).cuda()

        '''for summarizing reward'''
        self.episode_reward = {}
        self.final_reward = {}

        self.episode_reward['norm'] = 0.0
        self.episode_reward['bounty'] = 0.0
        self.episode_reward['bounty_clip'] = 0.0
        self.episode_reward['final'] = 0.0
        self.episode_reward['len'] = 0.0

        self.ext_reward = None

        if self.hierarchy_id in [0]:
            '''for hierarchy_id=0, we need to summarize reward_raw'''
            self.episode_reward['raw'] = 0.0
            self.episode_reward_raw_all = 0.0
            self.episode_count = 0.0

        '''initialize final_reward, since it is possible that the episode length is longer than num_steps'''
        for episode_reward_type in self.episode_reward.keys():
            self.final_reward[episode_reward_type] = self.episode_reward[episode_reward_type]

        '''try to load checkpoint'''
        try:
            self.num_trained_frames = np.load(args.save_dir+'/hierarchy_{}_num_trained_frames.npy'.format(self.hierarchy_id))[0]
            try:
                self.actor_critic.load_state_dict(torch.load(args.save_dir+'/hierarchy_{}_actor_critic.pth'.format(self.hierarchy_id)))
                print('[H-{:1}] Load actor_critic previous point: Successed'.format(self.hierarchy_id))
            except Exception as e:
                print('[H-{:1}] Load actor_critic previous point: Failed, due to {}'.format(self.hierarchy_id,e))
            if self.transition_model is not None:
                try:
                    self.transition_model.load_state_dict(torch.load(args.save_dir+'/hierarchy_{}_transition_model.pth'.format(self.hierarchy_id)))
                    print('[H-{:1}] Load transition_model previous point: Successed'.format(self.hierarchy_id))
                except Exception as e:
                    print('[H-{:1}] Load transition_model previous point: Failed, due to {}'.format(self.hierarchy_id,e))
            self.checkpoint_loaded = True
        except Exception as e:
            self.num_trained_frames = 0
            self.checkpoint_loaded = False

        print('[H-{:1}] Learner has been trained to step: {}'.format(self.hierarchy_id, self.num_trained_frames))
        self.num_trained_frames_at_start = self.num_trained_frames

        self.start = time.time()
        self.step_i = 0
        self.update_i = 0

        self.refresh_update_type()

        self.last_time_summarize_behavior = 0.0 # make sure the first episode is recorded
        self.summarize_behavior = False
        self.episode_visilize_stack = {}
        # self.episode_save_stack = {}

        self.predicted_next_observations_to_downer_layer = None
        self.mask_of_predicted_observation_to_downer_layer = None

        self.agent.set_this_layer(self)

        self.bounty_clip = torch.zeros(args.num_processes).cuda()
        self.reward_bounty_raw_to_return = torch.zeros(args.num_processes).cuda()
        self.reward_final = torch.zeros(args.num_processes).cuda()
        self.reward_bounty = torch.zeros(args.num_processes).cuda()

        if (self.args.env_name in ['Explore2D']) and (self.hierarchy_id in [0]):
            self.terminal_states = []

    def set_upper_layer(self, upper_layer):
        self.upper_layer = upper_layer
        self.agent.set_upper_layer(self.upper_layer)

    def step(self, inputs):
        '''as a environment, it has step method'''
        if args.reward_bounty > 0.0 and (not args.mutual_information):
            input_cpu_actions = inputs['actions_to_step']
            self.predicted_next_observations_by_upper_layer = inputs['predicted_next_observations_to_downer_layer']
            self.mask_of_predicted_observation_by_upper_layer = inputs['mask_of_predicted_observation_to_downer_layer']
            self.observation_predicted_from_by_upper_layer = inputs['observation_predicted_from_to_downer_layer']
            self.predicted_reward_bounty_by_upper_layer     = inputs['predicted_reward_bounty_to_downer_layer']
        else:
            input_cpu_actions = inputs['actions_to_step']
            self.predicted_next_observations_by_upper_layer = None
            self.observation_predicted_from_by_upper_layer = None
            self.predicted_reward_bounty_by_upper_layer = None

        '''convert: input_cpu_actions >> input_actions_onehot_global[self.hierarchy_id]'''
        input_actions_onehot_global[self.hierarchy_id].fill_(0.0)
        input_actions_onehot_global[self.hierarchy_id].scatter_(1,torch.from_numpy(input_cpu_actions).long().unsqueeze(1).cuda(),1.0)

        '''macro step forward'''
        reward_macro = None
        for macro_step_i in range(self.hierarchy_interval):

            self.is_final_step_by_upper_layer = (macro_step_i in [self.hierarchy_interval-1])
            if args.extend_driven > 0:
                self.is_extend_step = (macro_step_i%args.extend_driven==0)
            elif args.extend_driven == 0:
                self.is_extend_step = False
            else:
                raise NotImplementeds

            self.one_step()

            if reward_macro is None:
                reward_macro = self.reward
            else:
                reward_macro += self.reward

        return self.obs, reward_macro, self.reward_bounty_raw_to_return, self.done, self.info

    def one_step(self):
        '''as a environment, it has step method.
        But the step method step forward for args.hierarchy_interval times,
        as a macro action, this method is to step forward for a singel step'''

        '''for each one_step, interact with env for one step'''
        self.interact_one_step()

        self.step_i += 1
        if self.step_i==args.num_steps[self.hierarchy_id]:
            '''if reach args.num_steps[self.hierarchy_id], update agent for one step with the experiences stored in rollouts'''
            self.update_agent_one_step()
            self.step_i = 0

    def specify_action(self):
        '''this method is used to speicfy actions to the agent,
        so that we can get insight on with is happening'''

        if args.test_action:
            if self.hierarchy_id in [0]:
                human_action = 'nope'
                while human_action not in ['q','w','e','a','s','d']:
                    human_action = input(
                        '[Macro Action {}, actual action {}], Act: '.format(
                            utils.onehot_to_index(input_actions_onehot_global[0][0].cpu().numpy()),
                            self.action[0,0].item(),
                        )
                    )
                if args.env_name in ['MontezumaRevengeNoFrameskip-v4']:
                    human_action_map = {
                        'd':0,
                        'a':1,
                        's':2,
                        'w':3,
                        'e':4,
                        'q':5,
                    }
                    self.action[0,0] = int(human_action_map[human_action])
                else:
                    self.action[0,0] = int(human_action)

            if self.hierarchy_id in [2]:
                self.action[0,0] = int(
                    input(
                        '[top Action {}], Act: '.format(
                            self.action[0,0].item(),
                        )
                    )
                )

        # # DEBUG: specify higher level actions
        # if args.summarize_one_episode.split('_')[0] in ['sub']:
        #     if self.hierarchy_id in [1]:
        #         self.action[0,0]=int(args.summarize_one_episode.split('_')[1])
        #         print(self.action[:,0])

    def log_for_specify_action(self):

        if args.test_action and (self.hierarchy_id in [0]):

            print_str = ''
            print_str += '[reward {} ][done {}][masks {}]'.format(
                self.reward_raw_OR_reward[0],
                self.done[0],
                self.masks[0].item(),
            )
            if args.reward_bounty > 0.0:
                print_str += '[reward_bounty {}]'.format(
                    self.reward_bounty[0],
                )

            print(print_str)

    def generate_actions_to_step(self):
        '''this method generate actions_to_step controlled by many logic'''

        self.actions_to_step = self.action.squeeze(1).cpu().numpy()

        if self.hierarchy_id not in [0]:
            self.actions_to_step = {
                'actions_to_step': self.actions_to_step,
            }

        if (self.hierarchy_id not in [0]) and (args.reward_bounty > 0.0) and (not args.mutual_information):

            '''predict states'''
            self.transition_model.eval()
            with torch.no_grad():
                if len(self.rollouts.observations[self.step_i].size())==4:
                    '''state are represented in a image format'''
                    self.observation_predicted_from_to_downer_layer = self.rollouts.observations[self.step_i][:,-1:]
                elif len(self.rollouts.observations[self.step_i].size())==2:
                    '''state are represented in a one-dimentional vector format'''
                    self.observation_predicted_from_to_downer_layer = self.rollouts.observations[self.step_i]
                else:
                    raise NotImplemented

                if len(self.rollouts.observations[self.step_i].size()) in [4]:
                    now_states = self.rollouts.observations[self.step_i].repeat(self.envs.action_space.n,1,1,1)
                elif len(self.rollouts.observations[self.step_i].size()) in [2]:
                    now_states = self.rollouts.observations[self.step_i].repeat(self.envs.action_space.n,1)
                else:
                    raise NotImplemented

                self.predicted_next_observations_to_downer_layer, self.predicted_reward_bounty_to_downer_layer = self.transition_model(
                    inputs = now_states,
                    input_action = self.action_onehot_batch,
                )
                '''generate inverse mask'''
                if self.args.inverse_mask:
                    inverse_mask_model.eval()
                    self.mask_of_predicted_observation_to_downer_layer = inverse_mask_model.get_mask(
                        # last_states = (now_states[:,-1:]+self.predicted_next_observations_to_downer_layer),
                        last_states = now_states[:,-1:], # only predict from real obs first
                    )

            self.predicted_next_observations_to_downer_layer = self.predicted_next_observations_to_downer_layer.view(self.envs.action_space.n,args.num_processes,*self.predicted_next_observations_to_downer_layer.size()[1:])
            if self.args.inverse_mask:
                self.mask_of_predicted_observation_to_downer_layer = self.mask_of_predicted_observation_to_downer_layer.view(self.envs.action_space.n,args.num_processes,*self.mask_of_predicted_observation_to_downer_layer.size()[1:])
            else:
                self.mask_of_predicted_observation_to_downer_layer = None
            self.predicted_reward_bounty_to_downer_layer = self.predicted_reward_bounty_to_downer_layer.view(self.envs.action_space.n,args.num_processes,*self.predicted_reward_bounty_to_downer_layer.size()[1:]).squeeze(2)
            self.actions_to_step.update(
                {
                    'predicted_next_observations_to_downer_layer': self.predicted_next_observations_to_downer_layer,
                    'mask_of_predicted_observation_to_downer_layer': self.mask_of_predicted_observation_to_downer_layer,
                    'observation_predicted_from_to_downer_layer': self.observation_predicted_from_to_downer_layer,
                    'predicted_reward_bounty_to_downer_layer': self.predicted_reward_bounty_to_downer_layer,
                }
            )

    def generate_reward_bounty(self):
        '''this method generate reward bounty'''

        self.bounty_clip *= 0.0 # to record the clip value
        self.reward_bounty_raw_to_return *= 0.0  # to be return and train the bounty prediction
        self.reward_bounty *= 0.0 # bounty after clip
        self.reward_final *= 0.0 # reward be used to update policy

        '''START: computer normalized reward_bounty, EVERY T interval'''
        if (args.reward_bounty>0) and (self.hierarchy_id not in [args.num_hierarchy-1]) and (self.is_final_step_by_upper_layer or self.is_extend_step):

            '''START: compute none normalized reward_bounty_raw_to_return'''
            action_rb = self.rollouts.input_actions[self.step_i].nonzero()[:,1]
            obs_rb = self.obs.astype(float)-self.observation_predicted_from_by_upper_layer.cpu().numpy()
            prediction_rb = self.predicted_next_observations_by_upper_layer.cpu().numpy()
            if self.args.inverse_mask:
                mask_rb = self.mask_of_predicted_observation_by_upper_layer.cpu().numpy()
            for process_i in range(args.num_processes):
                difference_list = []
                for action_i in range(prediction_rb.shape[0]):
                    if action_i!=action_rb[process_i]:
                        '''compute difference'''
                        if args.distance in ['l2']:
                            if args.env_name in ['Explore2D']:
                                difference = np.linalg.norm(
                                    x = (obs_rb[process_i][0,0]-prediction_rb[action_i,process_i][0,0]),
                                    ord = 2,
                                )
                            elif ('MinitaurBulletEnv' in args.env_name) or ('AntBulletEnv' in args.env_name):
                                '''28:30 represents the position'''
                                difference = np.linalg.norm(
                                    x = (obs_rb[process_i][28:30]-prediction_rb[action_i,process_i][28:30]),
                                    ord = 2,
                                )/(obs_rb[process_i][28:30].shape[0]**0.5)
                            elif args.env_name in ['ReacherBulletEnv-v1','Explore2DContinuous']:
                                '''0:2 represents the position'''
                                difference = np.linalg.norm(
                                    x = (obs_rb[process_i][0:2]-prediction_rb[action_i,process_i][0:2]),
                                    ord = 2,
                                )/(obs_rb[process_i][0:2].shape[0]**0.5)
                            else:
                                raise NotImplemented
                        elif args.distance in ['mass_center']:
                            # mask here: *mask_rb[action_i,process_i][0]
                            difference = np.linalg.norm(
                                get_mass_center(obs_rb[process_i][0])-get_mass_center(prediction_rb[action_i,process_i][0])
                            )
                        else:
                            raise NotImplemented

                        difference_list += [difference*args.reward_bounty]
                if args.diversity_driven_active_function in ['min']:
                    self.reward_bounty_raw_to_return[process_i] += float(np.amin(difference_list))
                elif args.diversity_driven_active_function in ['sum']:
                    self.reward_bounty_raw_to_return[process_i] += float(np.sum(difference_list))
                else:
                    raise NotImplemented

            '''END: compute none normalized reward_bounty_raw_to_return'''

            '''mask reward bounty, since the final state is start state,
            and the estimation from transition model is not accurate'''
            self.reward_bounty_raw_to_return *= self.masks.squeeze()

            '''START: computer bounty after being clipped'''
            if args.clip_reward_bounty:
                for process_i in range(args.num_processes):
                    self.bounty_clip[process_i] = self.predicted_reward_bounty_by_upper_layer[action_rb[process_i]][process_i]
                    delta = (self.reward_bounty_raw_to_return[process_i]-self.bounty_clip[process_i])
                    if args.clip_reward_bounty_active_function in ['linear']:
                        self.reward_bounty[process_i] = delta
                    elif args.clip_reward_bounty_active_function in ['u']:
                        self.reward_bounty[process_i] = delta.sign().clamp(min=0.0,max=1.0)
                    elif args.clip_reward_bounty_active_function in ['relu']:
                        self.reward_bounty[process_i] = F.relu(delta)
                    elif args.clip_reward_bounty_active_function in ['shrink_relu']:
                        positive_active = delta.sign().clamp(min=0.0,max=1.0)
                        self.reward_bounty[process_i] = delta * positive_active + positive_active - 1
                    else:
                        raise Exception('No Supported')
            else:
                self.reward_bounty = self.reward_bounty_raw_to_return
            '''END: end of computer bounty after being clipped'''
        '''END: computer normalized reward_bounty'''

        '''START: compute reward_final for updating the policy'''
        self.reward_final += self.reward_bounty
        '''rewards added to reward_final in following part will NOT be normalized'''
        if args.reward_bounty>0:
            if self.hierarchy_id in [args.num_hierarchy-1]:
                '''top level only receive reward from env or nothing to observe unsupervised learning'''
                if self.args.env_name in ['OverCooked','GridWorld'] or ('NoFrameskip-v4' in args.env_name):
                    '''top level only receive reward from env'''
                    self.reward_final += self.reward.cuda()
                elif (self.args.env_name in ['MineCraft','Explore2D','Explore2DContinuous']) or ('Bullet' in args.env_name):
                    '''top level only receive nothing to observe unsupervised learning'''
                    pass
                else:
                    raise NotImplemented
            else:
                '''other levels except top level'''
                if (self.args.env_name in ['OverCooked','MineCraft','Explore2D','Explore2DContinuous']):
                    '''rewards occues less frequently or never occurs, down layers do not receive extrinsic reward'''
                    pass
                elif self.args.env_name in ['GridWorld','AntBulletEnv-v1'] or ('NoFrameskip-v4' in args.env_name):
                    '''reward occurs more frequently and we want down layers to know it'''
                    if self.args.env_name in ['GridWorld'] or ('NoFrameskip-v4' in args.env_name):
                        self.reward_final += self.reward.cuda()
                    elif self.args.env_name in ['AntBulletEnv-v1']:
                        self.reward_final += (self.reward.cuda()*0.001)
                    else:
                        raise NotImplemented
                else:
                    raise NotImplemented
        '''END: compute reward_final for updating the policy'''

        '''may mask to stop value function'''
        if args.reward_bounty>0:
            if not args.unmask_value_function:
                if self.is_final_step_by_upper_layer:
                    '''mask it and stop reward function'''
                    self.masks = self.masks * 0.0

    def interact_one_step(self):
        '''interact with self.envs for one step and store experience into self.rollouts'''

        self.rollouts.input_actions[self.step_i].copy_(input_actions_onehot_global[self.hierarchy_id])

        '''Sample actions'''
        with torch.no_grad():
            self.value, self.action, self.action_log_prob, self.states = self.actor_critic.act(
                inputs = self.rollouts.observations[self.step_i],
                states = self.rollouts.states[self.step_i],
                masks = self.rollouts.masks[self.step_i],
                deterministic = self.deterministic,
                input_action = self.rollouts.input_actions[self.step_i],
            )

        self.specify_action()

        self.generate_actions_to_step()

        '''Obser reward and next obs'''
        fetched = self.envs.step(self.actions_to_step)
        if self.hierarchy_id in [0]:
            # print('====')
            # print(self.obs[0])
            self.obs, self.reward_raw_OR_reward, self.done, self.info = fetched
            # print(self.obs[0])
            # print(self.done[0])
            # input('continue')
        else:
            self.obs, self.reward_raw_OR_reward, self.reward_bounty_raw_returned, self.done, self.info = fetched

        if self.hierarchy_id in [0]:
            if args.test_action:
                win_dic['Obs'] = viz.images(
                    self.obs[0],
                    win=win_dic['Obs'],
                    opts=dict(title='obs')
                )

        self.masks = torch.FloatTensor([[0.0] if done_ else [1.0] for done_ in self.done]).cuda()

        if self.hierarchy_id in [(args.num_hierarchy-1)]:
            '''top hierarchy layer is responsible for reseting env if all env has done'''
            if args.test_action:
                if self.masks[0] == 0.0:
                    self.obs = self.reset()
            else:
                if self.masks.sum() == 0.0:
                    self.obs = self.reset()

        if self.hierarchy_id in [0]:
            '''only when hierarchy_id is 0, the envs is returning reward_raw from the basic game emulator'''
            self.ext_reward = self.reward_raw_OR_reward
            self.reward_raw = torch.from_numpy(self.reward_raw_OR_reward*0).float()
            if args.env_name in ['OverCooked','MineCraft','GridWorld','Explore2D'] or ('NoFrameskip-v4' in args.env_name):
                self.reward = self.reward_raw.sign()
            elif ('Bullet' in args.env_name) or (args.env_name in ['Explore2DContinuous']):
                self.reward = self.reward_raw
            else:
                raise NotImplemented
        else:
            '''otherwise, this is reward'''
            self.reward = self.reward_raw_OR_reward

        self.generate_reward_bounty()

        self.log_for_specify_action()

        env_0_sleeping = self.envs.get_sleeping(env_index=0)
        if env_0_sleeping in [False]:
            self.step_summarize_from_env_0()
        elif env_0_sleeping in [True]:
            pass
        else:
            raise NotImplementedError

        '''If done then clean the history of observations'''
        if self.current_obs.dim() == 4:
            self.current_obs *= self.masks.unsqueeze(2).unsqueeze(2)
        else:
            self.current_obs *= self.masks

        self.update_current_obs(self.obs)

        if self.hierarchy_id not in [0]:
            self.rollouts.reward_bounty_raw[self.rollouts.step].copy_(self.reward_bounty_raw_returned.unsqueeze(1))

        self.rollouts.insert(
            self.current_obs,
            self.states,
            self.action,
            self.action_log_prob,
            self.value,
            self.reward_final.unsqueeze(1),
            self.masks,
        )

    def refresh_update_type(self):
        if args.reward_bounty > 0.0:

            if args.train_mode in ['together']:
                '''train_mode is together'''

                self.update_type = 'both'
                self.deterministic = False

            elif args.train_mode in ['switch']:
                '''train_mode is switch'''

                '''switch training between actor_critic and transition_model'''
                if self.update_i%2 == 1:
                    self.update_type = 'actor_critic'
                    self.deterministic = False
                else:
                    self.update_type = 'transition_model'
                    self.deterministic = True

            '''top layer do not have a transition_model'''
            if self.hierarchy_id in [args.num_hierarchy-1]:
                self.update_type = 'actor_critic'
                self.deterministic = False

        else:
            '''there is no transition_model'''

            self.update_type = 'actor_critic'
            self.deterministic = False

        '''overwrite if args.act_deterministically'''
        if args.act_deterministically:
            self.deterministic = True

    def update_agent_one_step(self):
        '''update the self.actor_critic with self.agent,
        according to the experiences stored in self.rollouts'''

        '''prepare rollouts for updating actor_critic'''
        if self.update_type in ['actor_critic','both']:
            with torch.no_grad():
                self.next_value = self.actor_critic.get_value(
                    inputs=self.rollouts.observations[-1],
                    states=self.rollouts.states[-1],
                    masks=self.rollouts.masks[-1],
                    input_action=self.rollouts.input_actions[-1],
                ).detach()
            self.rollouts.compute_returns(self.next_value, args.use_gae, args.gamma, args.tau)

        '''update, either actor_critic or transition_model'''
        epoch_loss = {}
        epoch_loss.update(
            self.agent.update(self.update_type)
        )
        if self.args.inverse_mask and (self.hierarchy_id in [0]):
            epoch_loss.update(
                update_inverse_mask_model(
                    bottom_layer=self,
                )
            )

        self.num_trained_frames += (args.num_steps[self.hierarchy_id]*args.num_processes)
        self.update_i += 1

        '''prepare rollouts for new round of interaction'''
        self.rollouts.after_update()

        if (self.args.env_name in ['Explore2D']) and (self.hierarchy_id in [0]):

            try:
                terminal_states_f = tables.open_file(
                    '{}/terminal_states.h5'.format(
                        args.save_dir,
                    ),
                    mode='a',
                    )

                for t_s in self.terminal_states:
                    terminal_states_f.root.data.append(t_s)

                self.terminal_states = []
                terminal_states_f.close()

            except Exception as e:
                print('Skip appending terminal_states')

        '''save checkpoint'''
        if (self.update_i % args.save_interval == 0 and args.save_dir != "") or (self.update_i in [1,2]):
            try:
                np.save(
                    args.save_dir+'/hierarchy_{}_num_trained_frames.npy'.format(self.hierarchy_id),
                    np.array([self.num_trained_frames]),
                )
                self.actor_critic.save_model(args.save_dir+'/hierarchy_{}_actor_critic.pth'.format(self.hierarchy_id))
                if self.transition_model is not None:
                    self.transition_model.save_model(args.save_dir+'/hierarchy_{}_transition_model.pth'.format(self.hierarchy_id))
                if self.args.inverse_mask and (self.hierarchy_id in [0]):
                    inverse_mask_model   .save_model(args.save_dir+'/inverse_mask_model.pth')
                print("[H-{:1}] Save checkpoint successed.".format(self.hierarchy_id))
            except Exception as e:
                print("[H-{:1}] Save checkpoint failed, due to {}.".format(self.hierarchy_id,e))

        '''print info'''
        if self.update_i % args.log_interval == 0:
            self.end = time.time()

            print_string = "[H-{:1}][{:9}/{}], FPS {:4}".format(
                self.hierarchy_id,
                self.num_trained_frames, args.num_frames,
                int((self.num_trained_frames-self.num_trained_frames_at_start) / (self.end - self.start)),
            )
            print_string += ', final_reward '

            for episode_reward_type in self.final_reward.keys():
                print_string += '[{}:{:8.2f}]'.format(
                    episode_reward_type,
                    self.final_reward[episode_reward_type]
                )

            if self.hierarchy_id in [0]:
                print_string += ', remaining {:4.1f} hours'.format(
                    (self.end - self.start)/(self.num_trained_frames-self.num_trained_frames_at_start)*(args.num_frames-self.num_trained_frames)/60.0/60.0,
                )
            if self.args.summarize_behavior:
                print_string += ', summarize_behavior {}'.format(
                    self.summarize_behavior,
                )
            print(print_string)

        '''visualize results'''
        if (self.update_i % args.vis_curves_interval == 0) and (not args.test_action):
            '''we use tensorboard since its better when comparing plots'''
            self.summary = tf.Summary()
            if args.env_name in ['OverCooked']:
                action_count = np.zeros(4)
                for info_index in range(len(self.info)):
                    action_count += self.info[info_index]['action_count']
                if args.see_leg_fre:
                    leg_count = np.zeros(17)
                    for leg_index in range(len(self.info)):
                        leg_count += self.info[leg_index]['leg_count']

            if args.env_name in ['OverCooked']:
                if self.hierarchy_id in [0]:
                    for index_action in range(4):
                        self.summary.value.add(
                            tag = 'hierarchy_{}/action_{}'.format(
                                0,
                                index_action,
                            ),
                            simple_value = action_count[index_action],
                        )
                    if args.see_leg_fre:
                        for index_leg in range(17):
                            self.summary.value.add(
                                tag = 'hierarchy_{}/leg_{}_in_one_eposide'.format(
                                    0,
                                    index_leg,
                                ),
                                simple_value = leg_count[index_leg],
                            )

            if self.hierarchy_id in [0] and self.ext_reward is not None:
                self.summary.value.add(
                    tag = 'hierarchy_{}/final_reward_{}'.format(
                        self.hierarchy_id,
                        'ext',
                    ),
                    simple_value = self.ext_reward[0],
                )

            for episode_reward_type in self.episode_reward.keys():
                self.summary.value.add(
                    tag = 'hierarchy_{}/final_reward_{}'.format(
                        self.hierarchy_id,
                        episode_reward_type,
                    ),
                    simple_value = self.final_reward[episode_reward_type],
                )

            for epoch_loss_type in epoch_loss.keys():
                self.summary.value.add(
                    tag = 'hierarchy_{}/epoch_loss_{}'.format(
                        self.hierarchy_id,
                        epoch_loss_type,
                    ),
                    simple_value = epoch_loss[epoch_loss_type],
                )

            summary_writer.add_summary(self.summary, self.num_trained_frames)
            summary_writer.flush()

        '''update system status'''
        self.refresh_update_type()

        '''check end condition'''
        if self.hierarchy_id in [0]:
            '''if hierarchy_id is 0, it is the basic env, then control the training
            progress by its num_trained_frames'''
            if self.num_trained_frames > args.num_frames:
                raise Exception('Done')

    def reset(self):
        '''as a environment, it has reset method'''
        self.obs = self.envs.reset()
        if self.hierarchy_id in [0]:
            if args.test_action:
                win_dic['Obs'] = viz.images(
                    self.obs[0],
                    win=win_dic['Obs'],
                    opts=dict(title='obs')
                )
        self.update_current_obs(self.obs)
        self.rollouts.observations[0].copy_(self.current_obs)
        return self.obs

    def update_current_obs(self, obs):
        '''update self.current_obs, which contains args.num_stack frames, with obs, which is current frame'''
        shape_dim0 = self.envs.observation_space.shape[0]
        obs = torch.from_numpy(obs).float()
        if args.num_stack > 1:
            self.current_obs[:, :-shape_dim0] = self.current_obs[:, shape_dim0:]
        self.current_obs[:, -shape_dim0:] = obs

    def step_summarize_from_env_0(self):

        if (((time.time()-self.last_time_summarize_behavior)/60.0) > args.summarize_behavior_interval) and (not (args.test_action)) and args.summarize_behavior:
            '''log behavior every x minutes'''
            if self.episode_reward['len']==0:
                self.summarize_behavior = True

        if self.summarize_behavior:
            self.summarize_behavior_at_step()

        '''summarize reward'''
        self.episode_reward['norm'] += self.reward[0].item()
        self.episode_reward['bounty'] += self.reward_bounty[0].item()
        self.episode_reward['bounty_clip'] += self.bounty_clip[0].item()
        self.episode_reward['final'] += self.reward_final[0].item()
        if self.hierarchy_id in [0]:
            '''for hierarchy_id=0, summarize reward_raw'''
            self.episode_reward['raw'] += self.reward_raw[0].item()

        self.episode_reward['len'] += 1

        if self.done[0]:

            for episode_reward_type in self.episode_reward.keys():
                self.final_reward[episode_reward_type] = self.episode_reward[episode_reward_type]
                self.episode_reward[episode_reward_type] = 0.0

            if self.hierarchy_id in [0]:
                self.episode_reward_raw_all += self.final_reward['raw']
                self.episode_count += 1
                self.final_reward['raw_all'] = self.episode_reward_raw_all / self.episode_count

            if self.summarize_behavior:
                self.summarize_behavior_at_done()
                self.last_time_summarize_behavior = time.time()
                self.summarize_behavior = False

            if (self.args.env_name in ['Explore2D']) and (self.hierarchy_id in [0]):
                self.terminal_states += [self.obs[0,0,0:1]]

    def summarize_behavior_at_step(self):

        '''summarize observation'''
        if args.summarize_observation:
            state_img = obs_to_state_img(self.obs[0])
            try:
                self.episode_visilize_stack['observation'] += [state_img]
                '''
                    0-255, uint8, either (xx,xx) for gray image or (xx,xx,3) for rgb image.
                '''
            except Exception as e:
                self.episode_visilize_stack['observation'] = [state_img]

        '''summarize rendered observation'''
        if self.args.summarize_rendered_behavior:
            if self.hierarchy_id in [0]:
                rendered_observation = self.envs.get_one_render(env_index=0)
                rendered_observation = puton_input_action_text(rendered_observation)
                try:
                    self.episode_visilize_stack['rendered_observation'] += [rendered_observation]
                except Exception as e:
                    self.episode_visilize_stack['rendered_observation'] = [rendered_observation]

        '''Summery state_prediction'''
        if args.summarize_state_prediction:
            if self.predicted_next_observations_to_downer_layer is not None:
                img = obs_to_state_img(self.observation_predicted_from_to_downer_layer[0].cpu().numpy())
                for action_i in range(self.envs.action_space.n):
                    if state_type in ['standard_image']:
                        temp = ((self.predicted_next_observations_to_downer_layer[action_i,0]+255.0)/2.0)
                    elif state_type in ['vector']:
                        temp = self.observation_predicted_from_to_downer_layer[0] + self.predicted_next_observations_to_downer_layer[action_i,0]
                    temp = obs_to_state_img(
                        temp.cpu().numpy(),
                        marker = "+",
                        c = 'green',
                    )

                    if (args.env_name in ['Explore2D','Explore2DContinuous']) or ('Bullet' in args.env_name):
                        img = img+temp
                    elif args.env_name in ['OverCooked','MineCraft','GridWorld'] or ('NoFrameskip-v4' in args.env_name):
                        img = np.concatenate((img,temp),1)
                    else:
                        raise NotImplemented

                    if self.args.inverse_mask:
                        inverse_model_mask = self.mask_of_predicted_observation_to_downer_layer[action_i,0,:,:,:]
                        img = np.concatenate(
                            (
                                img,
                                obs_to_state_img(
                                    (binarize_mask_torch(inverse_model_mask)*255.0).cpu().numpy()
                                )
                            ),
                            1,
                        )
                if (args.env_name in ['Explore2D','Explore2DContinuous']) or ('Bullet' in args.env_name):
                    img = (img/np.amax(img)*255.0).astype(np.uint8)

                elif args.env_name in ['OverCooked','MineCraft','GridWorld'] or ('NoFrameskip-v4' in args.env_name):
                    pass
                else:
                    raise NotImplemented

                try:
                    self.episode_visilize_stack['state_prediction'] += [img]
                except Exception as e:
                    self.episode_visilize_stack['state_prediction'] = [img]

        # if self.hierarchy_id in [0]:
        #     '''record actions'''
        #     try:
        #         self.episode_save_stack['actions'] += [self.action[0,0].item()]
        #     except Exception as e:
        #         self.episode_save_stack['actions'] = [self.action[0,0].item()]

    def summarize_behavior_at_done(self):

        if args.summarize_one_episode not in ['None']:
            log_header = '{}_'.format(args.summarize_one_episode)
        else:
            log_header = ''

        '''log episode_visilize_stack with avi'''
        for episode_visilize_stack_name in self.episode_visilize_stack.keys():

            self.episode_visilize_stack[episode_visilize_stack_name] = np.stack(
                self.episode_visilize_stack[episode_visilize_stack_name]
            )

            '''log everything with video'''
            videoWriter = cv2.VideoWriter(
                '{}/{}H-{}_F-{}_{}.avi'.format(
                    args.save_dir,
                    log_header,
                    self.hierarchy_id,
                    self.num_trained_frames,
                    episode_visilize_stack_name,
                ),
                log_fourcc,
                log_fps,
                (self.episode_visilize_stack[episode_visilize_stack_name].shape[2],self.episode_visilize_stack[episode_visilize_stack_name].shape[1]),
            )
            for frame_i in range(self.episode_visilize_stack[episode_visilize_stack_name].shape[0]):
                cur_frame = self.episode_visilize_stack[episode_visilize_stack_name][frame_i]
                if len(cur_frame.shape)==2:
                    '''gray image'''
                    cur_frame = cv2.cvtColor(cur_frame, cv2.cv2.COLOR_GRAY2RGB)
                elif len(cur_frame.shape)==3:
                    pass
                else:
                    raise NotImplemented
                videoWriter.write(cur_frame.astype(np.uint8))
            videoWriter.release()

            self.episode_visilize_stack[episode_visilize_stack_name] = None

        # '''log episode_save_stack with npy'''
        # for episode_save_stack_name in self.episode_save_stack.keys():
        #
        #     self.episode_save_stack[episode_save_stack_name] = np.stack(
        #         self.episode_save_stack[episode_save_stack_name]
        #     )
        #
        #     np.save(
        #         '{}/{}H-{}_F-{}_{}.npy'.format(
        #             args.save_dir,
        #             log_header,
        #             self.hierarchy_id,
        #             self.num_trained_frames,
        #             episode_save_stack_name,
        #         ),
        #         self.episode_save_stack[episode_save_stack_name],
        #     )
        #
        #     self.episode_save_stack[episode_save_stack_name] = None

        '''log world with sav, just for MineCraft'''
        if self.hierarchy_id in [0] and self.args.env_name in ['MineCraft']:
            self.envs.unwrapped.saveWorld(
                saveGameFile = '{}/{}H-{}_F-{}_savegame.sav'.format(
                    args.save_dir,
                    log_header,
                    self.hierarchy_id,
                    self.num_trained_frames,
                )
            )

        print('[H-{:1}] Log behavior done at {}.'.format(
            self.hierarchy_id,
            self.num_trained_frames,
        ))

        if args.summarize_one_episode not in ['None']:
            print('summarize_one_episode done')
            raise SystemExit

    def get_sleeping(self, env_index):
        return self.envs.get_sleeping(env_index)

def main():

    hierarchy_layer = []
    hierarchy_layer += [HierarchyLayer(
        envs = bottom_envs,
        hierarchy_id = 0,
    )]
    for hierarchy_i in range(1, args.num_hierarchy):
        hierarchy_layer += [HierarchyLayer(
            envs = hierarchy_layer[hierarchy_i-1],
            hierarchy_id=hierarchy_i,
        )]

    for hierarchy_i in range(0,args.num_hierarchy-1):
        hierarchy_layer[hierarchy_i].set_upper_layer(hierarchy_layer[hierarchy_i+1])

    hierarchy_layer[-1].reset()

    while True:

        '''as long as the top hierarchy layer is stepping forward,
        the downer layers is controlled and kept running.
        Note that the top hierarchy does no have to call step,
        calling one_step is enough'''
        hierarchy_layer[-1].predicted_next_observations_by_upper_layer = None
        hierarchy_layer[-1].mask_of_predicted_observation_by_upper_layer = None
        hierarchy_layer[-1].observation_predicted_from_by_upper_layer = None
        hierarchy_layer[-1].predicted_reward_bounty_by_upper_layer = None
        hierarchy_layer[-1].is_final_step_by_upper_layer = False
        hierarchy_layer[-1].is_extend_step = False
        hierarchy_layer[-1].one_step()

if __name__ == "__main__":
    main()
