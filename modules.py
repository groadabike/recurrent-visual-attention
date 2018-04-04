import torch
import torch.nn as nn
import torch.nn.functional as F

from torch.autograd import Variable

import numpy as np


class retina(object):
    def __init__(self, patch_size, num_patches, scale):
        """
        @param patch_size: side length of the extracted patched.
        @param num_patches: number of patches to extract in the glimpse.
        @param scale: scaling factor that controls the size of successive patches.
        """
        self.patch_size = patch_size
        self.num_patches = num_patches
        self.scale = scale

    def foveate(self, x, l):
        """
        Extract `num_patches` square patches,  centered at location `l`.
        The initial patch is a square of sidelength `patch_size`,
        and each subsequent patch is a square whose sidelength is `scale`
        times the size of the previous patch.  All patches are finally
        resized to the same size of the first patch and then flattened.

        @param x: img. (batch, height, width, channel)
        @param l: location. (batch,2)
        @return Variable: (batch, num_patches*channel*patch_size*patch_size).
        """
        patches = []
        size = self.patch_size

        # extract num_patches patches of increasing size
        for i in range(self.num_patches):
            patches.append(self.extract_patch(x, l, size))
            size = int(self.scale * size)

        # resize the patches to squares of size patch_size
        for i in range(1, len(patches)):
            num_patches = patches[i].shape[-1] // self.patch_size
            patches[i] = F.avg_pool2d(patches[i], num_patches)

        # concatenate into a single tensor and flatten
        patches = torch.cat(patches, 1)
        patches = patches.view(patches.shape[0], -1)

        return patches

    def extract_patch(self, x, l, size):
        """
        @param x: img. (batch, height, width, channel)
        @param l: location. (batch, 2)
        @param size: the size of the extracted patch.
        @return Variable (batch, height, width, channel)
        """
        B, C, H, W = x.shape

        if not hasattr(self, 'imgShape'):
            self.imgShape = torch.FloatTensor([H, W]).unsqueeze(0)

        # coordins from [-1,1] to H,W scale
        coords = (0.5 * ((l.data + 1.0) * self.imgShape)).long()

        # pad the image with enough 0s
        x = nn.ConstantPad2d(size//2, 0.)(x)

        # calculate coordinate for each batch samle (padding considered)
        from_x, from_y = coords[:, 0], coords[:, 1]
        to_x, to_y = from_x + size, from_y + size
        # The above is the original implementation
        # It only works if the input image is a square
        # The following is the correct implementation
        # from_y, from_x = coords[:, 0], coords[:, 1]
        # to_y, to_x = from_y + size, from_x + size

        # extract the patches
        patch = []
        for i in range(B):
            patch.append(x[i, :, from_y[i]:to_y[i], from_x[i]:to_x[i]].unsqueeze(0))

        return torch.cat(patch)


class GlimpseNet(nn.Module):
    def __init__(self, hidden_g, hidden_l, patch_size, num_patches, scale, num_channel):
        """
        @param hidden_g: hidden layer size of the fc layer for `phi`.
        @param hidden_l: hidden layer size of the fc layer for `l`.
        @param patch_size: size of the square patches in the glimpses extracted
        @param by the retina.
        @param num_patches: number of patches to extract per glimpse.
        @param scale: scaling factor that controls the size of successive patches.
        @param num_channel: number of channels in each image.
        """
        super(GlimpseNet, self).__init__()
        self.retina = retina(patch_size, num_patches, scale)

        # glimpse layer
        D_in = num_patches*patch_size*patch_size*num_channel
        self.fc1 = nn.Linear(D_in, hidden_g)

        # location layer
        self.fc2 = nn.Linear(2, hidden_l)

        self.fc3 = nn.Linear(hidden_g, hidden_g+hidden_l)
        self.fc4 = nn.Linear(hidden_l, hidden_g+hidden_l)

    def forward(self, x_t, l_t):
        """
        Combines the "what" and the "where" into a glimpse feature vector. Extract `num_patches` different resolution patches of the same size (patch_size, patch_size) to get "what". Then combine it with a two dimension "where" vector, each of which element is ranging in [-1,1].

        @param x_t: (batch, height, width, channel)
        @param l_t: (batch, 2)
        @return output: (batch, hidden_g+hidden_l)
        """
        glimpse = self.retina.foveate(x_t, l_t)

        what = self.fc3(F.relu(self.fc1(glimpse)))
        where = self.fc4(F.relu(self.fc2(l_t)))

        g = F.relu(what + where)

        return g


class core_network(nn.Module):
    """
    An RNN that maintains an internal state that integrates
    information extracted from the history of past observations.
    It encodes the agent's knowledge of the environment through
    a state vector `h_t` that gets updated at every time step `t`.

    Concretely, it takes the glimpse representation `g_t` as input,
    and combines it with its internal state `h_t_prev` at the previous
    time step, to produce the new internal state `h_t` at the current
    time step.

    In other words:

        `h_t = relu( fc(h_t_prev) + fc(g_t) )`

    Args
    ----
    - input_size: input size of the rnn.
    - hidden_size: hidden size of the rnn.
    - g_t: a 2D tensor of shape (B, hidden_size). The glimpse
      representation returned by the glimpse network for the
     current timestep `t`.
    - h_t_prev: a 2D tensor of shape (B, hidden_size). The
      hidden state vector for the previous timestep `t-1`.

    Returns
    -------
    - h_t: a 2D tensor of shape (B, hidden_size). The hidden
      state vector for the current timestep `t`.
    """
    def __init__(self, input_size, hidden_size):
        super(core_network, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size

        self.i2h = nn.Linear(input_size, hidden_size)
        self.h2h = nn.Linear(hidden_size, hidden_size)

    def forward(self, g_t, h_t_prev):
        h1 = self.i2h(g_t)
        h2 = self.h2h(h_t_prev)
        h_t = F.relu(h1 + h2)
        return h_t

    def init_state(self, batch_size, use_gpu=False):
        """
        Initialize the hidden state of the core network
        and the location vector.

        This is called once every time a new minibatch
        `x` is introduced.
        """
        dtype = torch.cuda.FloatTensor if use_gpu else torch.FloatTensor
        h_t = torch.zeros(batch_size, self.hidden_size)
        h_t = Variable(h_t).type(dtype)

        l_t = torch.Tensor(batch_size, 2).uniform_(-1, 1)
        l_t = Variable(l_t).type(dtype)

        return h_t, l_t


class ActionNet(nn.Module):
    """
    Uses the internal state `h_t` of the core network to
    produce the final output classification.

    Concretely, feeds the hidden state `h_t` through a fc
    layer followed by a softmax to create a vector of
    output probabilities over the possible classes.

    Hence, the environment action `a_t` is drawn from a
    distribution conditioned on an affine transformation
    of the hidden state vector `h_t`, or in other words,
    the action network is simply a linear softmax classifier.

    Args
    ----
    - input_size: input size of the fc layer.
    - output_size: output size of the fc layer.
    - h_t: the hidden state vector of the core network for
      the current time step `t`.

    Returns
    -------
    - a_t: output probability vector over the classes.
    """
    def __init__(self, input_size, output_size):
        super(ActionNet, self).__init__()
        self.fc = nn.Linear(input_size, output_size)

    def forward(self, h_t):
        a_t = F.log_softmax(self.fc(h_t), dim=1)
        return a_t


class LocationNet(nn.Module):
    def __init__(self, input_size, output_size, std):
        """
        @param input_size: input size of the fc layer.
        @param output_size: output size of the fc layer.
        @param std: standard deviation of the normal distribution.
        """
        super(LocationNet, self).__init__()
        self.std = std
        self.fc = nn.Linear(input_size, output_size)

    def forward(self, h_t):
        """
        Generate next location `l_t` by calculating the coordinates
        conditioned on an affine and adding a normal noise followed
        by a tanh to clamp the output beween [-1, 1].
        @param h_t: hidden state. (batch, hidden_size)
        @return mu: noise free location. Used for calculating
                    reinforce loss. (B, 2).
        @return l_t: Next location. (B, 2).
        """
        # compute mean
        mu = F.tanh(self.fc(h_t))

        # sample from gaussian parametrized by this mean
        # This is the origin repo implementation
        noise = torch.from_numpy(np.random.normal(
            scale=self.std, size=mu.shape)
        )
        noise = Variable(noise.float()).type_as(mu)

        # # This is an equivalent implementation
        # noise = torch.zeros_like(mu)
        # noise.data.normal_(std=self.std)

        l_t = mu + noise

        # bound between [-1, 1]
        l_t = F.tanh(l_t)

        # prevent gradient flow
        l_t = l_t.detach()

        return mu, l_t


class BaselineNet(nn.Module):
    """
    Regresses the baseline in the reward function
    to reduce the variance of the gradient update.

    Args
    ----
    - input_size: input size of the fc layer.
    - output_size: output size of the fc layer.
    - h_t: the hidden state vector of the core network
      for the current time step `t`.

    Returns
    -------
    - b_t: a 2D vector of shape (B, 1). The baseline
      for the current time step `t`.
    """
    def __init__(self, input_size, output_size):
        super(BaselineNet, self).__init__()
        self.fc = nn.Linear(input_size, output_size)

    def forward(self, h_t):
        b_t = F.relu(self.fc(h_t))
        return b_t
