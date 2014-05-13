__author__ = 'mdenil'

import numpy as np

import cpu.space
import collections

import psutil


class Linear(object):
    def __init__(self, n_input, n_output, W=None):
        self.n_input = n_input
        self.n_output = n_output

        if W is None:
            self.W = 0.1 * np.random.standard_normal(size=(self.n_input, self.n_output))
        else:
            assert W.shape == (n_input, n_output)
            self.W = W

    def fprop(self, X, meta):

        X, X_space = meta['space_below'].transform(X, ('b', ('d', 'f', 'w')))

        Y, Y_space = self._fprop(X)

        assert Y_space.is_compatible_shape(Y)

        lengths_below = meta['lengths']
        meta['lengths'] = np.ones_like(meta['lengths'])
        meta['space_above'] = Y_space

        fprop_state = {
            'X': X,
            'X_space': X_space,
            'lengths_below': lengths_below.copy(),
            }

        return Y, meta, fprop_state

    def bprop(self, delta, meta, fprop_state):
        delta, delta_space = meta['space_above'].transform(delta, ('b', 'd'))

        out = self._bprop(delta)

        meta['space_below'] = fprop_state['X_space']
        meta['lengths'] = fprop_state['lengths_below']
        return out, meta

    def grads(self, delta, meta, fprop_state):
        X = fprop_state['X']
        X_space = fprop_state['X_space']
        X, X_space = X_space.transform(X, ('b', ('d', 'f', 'w')))

        delta, delta_space = meta['space_above'].transform(delta, ('b', ('d','f','w')))

        return self._grads(X, delta)

    def params(self):
        return [self.W]

    def __repr__(self):
        return "{}(W={})".format(
            self.__class__.__name__,
            self.W.shape)


class Softmax(object):
    def __init__(self,
                 n_classes,
                 n_input_dimensions,
                 W=None,
                 b=None):
        self.n_classes = n_classes
        self.n_input_dimensions = n_input_dimensions

        if W is None:
            self.W = 0.1 * np.random.standard_normal(size=(self.n_input_dimensions, self.n_classes))
        else:
            assert W.shape == (self.n_input_dimensions, self.n_classes)
            self.W = W

        if b is None:
            self.b = np.zeros(shape=(1, self.n_classes))
        else:
            assert b.shape == (1, self.n_classes)
            self.b = b

    def fprop(self, X, meta):

        X, X_space = meta['space_below'].transform(X, ('b', ('d', 'f', 'w')))

        if not X.shape[1] == self.W.shape[0]:
            raise ValueError("Cannot multiply X.shape={} ({}) with W.shape={}".format(X.shape, X_space, self.W.shape))

        Y = self._fprop(X, X_space)

        Y_space = X_space.without_axes(('w', 'f'))
        Y_space = Y_space.with_extents(d=self.n_classes)

        lengths_below = meta['lengths']
        # TODO: what is the length of a sentence with no w dimension anyway?
        meta['lengths'] = np.ones_like(meta['lengths'])
        meta['space_above'] = Y_space

        fprop_state = {
            'X_space': X_space,
            'Y_space': Y_space,
            'lengths_below': lengths_below.copy(),
            'X': X,
            'Y': Y,
        }

        return Y, meta, fprop_state

    def bprop(self, delta, meta, fprop_state):
        Y = fprop_state['Y']
        assert fprop_state['Y_space'].is_compatible_shape(Y)

        delta, delta_space = meta['space_above'].transform(delta, ('b', 'd'))

        out = self._bprop(delta, Y)

        meta['space_below'] = fprop_state['X_space']
        meta['lengths'] = fprop_state['lengths_below']
        return out, meta

    def grads(self, delta, meta, fprop_state):
        X = fprop_state['X']
        Y = fprop_state['Y']
        X_space = fprop_state['X_space']
        X, X_space = X_space.transform(X, ('b', ('d', 'f', 'w')))

        delta, delta_space = meta['space_above'].transform(delta, ('b', ('d', 'f', 'w')))

        [grad_W, grad_b] = self._grads(delta, X, Y)

        return [grad_W, grad_b]

    def params(self):
        return [self.W, self.b]

    def __repr__(self):
        return "{}(W={}, b={})".format(
            self.__class__.__name__,
            self.W.shape,
            self.b.shape)


class SentenceConvolution(object):
    def __init__(self,
                 n_feature_maps,
                 kernel_width,
                 n_input_dimensions,
                 n_channels,
                 n_threads=psutil.NUM_CPUS,
                 W=None):

        self.n_feature_maps = n_feature_maps
        self.kernel_width = kernel_width
        self.n_input_dimensions = n_input_dimensions
        self.n_channels = n_channels
        self.n_threads = n_threads

        if W is None:
            self.W = 0.1 * np.random.standard_normal(
                size=(n_input_dimensions, n_feature_maps, n_channels, kernel_width))
            self._kernel_space = cpu.space.CPUSpace.infer(self.W, ('d', 'f', 'c', 'w'))
            self.W, self._kernel_space = self._kernel_space.transform(self.W, [('d', 'b', 'f', 'c'), 'w'])
        else:
            # :(
            assert W.shape == (n_input_dimensions * n_feature_maps * n_channels, kernel_width)
            self.W = W
            self._kernel_space = cpu.space.CPUSpace(
                (('d', 'b', 'f', 'c'), 'w'),
                collections.OrderedDict([
                    ('d', n_input_dimensions),
                    ('b', 1),
                    ('f', n_feature_maps),
                    ('c', n_channels),
                    ('w', kernel_width)
                ]))

    def fprop(self, X, meta):

        # Things go wrong if the w extent of X is smaller than the kernel width... for now just don't do that.
        if meta['space_below'].get_extent('w') < self.kernel_width:
            raise ValueError("SentenceConvolution does not support input with w={} extent smaller than kernel_width={}".format(
                meta['space_below'].get_extent('w'),
                self.kernel_width
            ))

        working_space = meta['space_below']
        lengths = meta['lengths']

        # features in the input space become channels here
        if 'f' in working_space.folded_axes:
            working_space = working_space.rename_axes(f='c')
        else:
            X, working_space = working_space.add_axes(X, 'c')

        fprop_state = {
            'input_space': working_space,
            'X': X,
            'lengths_below': lengths.copy()
        }

        d, b, c, w = working_space.get_extents(('d', 'b', 'c', 'w'))

        if not self.n_channels == c:
            raise ValueError("n_chanels={} but the data has {} channels.".format(self.n_channels, c))
        if not self.n_input_dimensions == d:
            raise ValueError("n_input_dimensions={} but the data has {} dimensions.".format(self.n_input_dimensions, d))
        f = self.n_feature_maps

        X, working_space = working_space.transform(X, (('d', 'b', 'f', 'c'), 'w'), f=f)

        X, working_space = self._fprop(X, working_space)

        # length of a wide convolution
        lengths = lengths + self.kernel_width - 1

        meta['space_above'] = working_space
        meta['lengths'] = lengths

        return X, meta, fprop_state

    def bprop(self, delta, meta, fprop_state):
        working_space = meta['space_above']
        lengths = meta['lengths']
        X_space = fprop_state['input_space']

        delta, working_space = working_space.transform(
            delta,
            (('d', 'b', 'f', 'c'), 'w'),
            c=X_space.get_extent('c'))

        delta, working_space = self._bprop(delta, working_space)

        lengths = lengths - self.kernel_width + 1

        meta['space_below'] = working_space
        meta['lengths'] = lengths

        return delta, meta

    def grads(self, delta, meta, fprop_state):
        delta_space = meta['space_above']
        X = fprop_state['X']
        X_space = fprop_state['input_space']

        delta, delta_space = delta_space.transform(
            delta,
            (('d', 'b', 'f', 'c'), 'w'),
            c=X_space.get_extent('c'))
        X, X_space = X_space.transform(
            X,
            (('d', 'b', 'f', 'c'), 'w'),
            f=delta_space.get_extent('f'))

        return self._grads(delta, delta_space, X)

    def params(self):
        return [self.W]

    def __repr__(self):
        return "{}(W={})".format(
            self.__class__.__name__,
            self.W.shape)




class Bias(object):
    def __init__(self, n_input_dims, n_feature_maps, b=None):
        self.n_input_dims = n_input_dims
        self.n_feature_maps = n_feature_maps

        if b is None:
            self.b = np.zeros((n_input_dims, n_feature_maps))
        else:
            assert b.shape == (n_input_dims, n_feature_maps)
            self.b = b

    def fprop(self, X, meta):
        working_space = meta['space_below']

        if not self.n_input_dims == working_space.get_extent('d'):
            raise ValueError("n_input_dims={} but input has {} dimensions.".format(self.n_input_dims, working_space.get_extent('d')))
        if not self.n_feature_maps == working_space.get_extent('f'):
            raise ValueError("n_feature_maps={} but input has {} features.".format(self.n_feature_maps, working_space.get_extent('f')))

        X, working_space = working_space.transform(X, ('d', 'b', 'f', 'w'))

        X = self._fprop(X, working_space)

        meta['space_above'] = working_space

        fprop_state = {
            'lengths_above': meta['lengths'].copy() # for debugging only
        }

        return X, meta, fprop_state

    def bprop(self, delta, meta, fprop_state):
        assert np.all(meta['lengths'] == fprop_state['lengths_above'])
        meta['space_below'] = meta['space_above']
        return delta, meta

    def grads(self, delta, meta, fprop_state):
        working_space = meta['space_above']
        return self._grads(delta, working_space)

    def params(self):
        return [self.b]

    def __repr__(self):
        return "{}(n_input_dims={}, n_feature_maps={})".format(
            self.__class__.__name__,
            self.n_input_dims,
            self.n_feature_maps)
