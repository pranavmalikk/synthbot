# Copyright 2018 Google, Inc.,
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utilities used elsewhere in this library."""

import functools
import sonnet as snt
import tensorflow as tf
from tensorflow.contrib import distributions


def calc_kl(hparams, a_sample, dist_a, dist_b):
    """Calculates KL(a||b), either analytically or via MC estimate."""
    if hparams.use_monte_carlo_kl:
        return dist_a.log_prob(a_sample) - dist_b.log_prob(a_sample)
    return distributions.kl_divergence(dist_a, dist_b)


def activation(hparams):
    """Returns the activation function selected in hparams."""
    return {
        "relu": tf.nn.relu,
        "elu": tf.nn.elu,
    }[hparams.activation]


def positive_projection(hparams):
    """Returns the positive projection selected in hparams."""
    proj = {
        "exp": tf.exp,
        "softplus": tf.nn.softplus,
    }[hparams.positive_projection]
    return lambda tensor: proj(tensor) + hparams.positive_eps


def regularizer(hparams):
    def _apply_regularizer(tensor):
        with tf.control_dependencies(None):
            return tf.contrib.layers.l1_l2_regularizer(
                scale_l1=hparams.l1_regularization,
                scale_l2=hparams.l2_regularization)(tensor)
    return _apply_regularizer

def make_rnn(hparams, name):
    """Constructs a DeepRNN using hparams.rnn_hidden_sizes."""
    regularizers = {
        snt.LSTM.W_GATES: regularizer(hparams)
    }
    with tf.variable_scope(name):
        layers = [snt.LSTM(size, regularizers=regularizers)
                  for size in hparams.rnn_hidden_sizes]
        return snt.DeepRNN(layers, skip_connections=False, name=name)


def make_mlp(hparams, layers, name=None, **kwargs):
    """Constructs an MLP with the given layers, using hparams.activation."""
    regularizers = {
        "w": regularizer(hparams)
    }
    return snt.nets.MLP(
        layers,
        activation=activation(hparams),
        regularizers=regularizers,
        name=name or "MLP",
        **kwargs)


def concat_features(tensors):
    """Concatenates nested tensors along the last dimension."""
    tensors = snt.nest.flatten(tensors)
    if len(tensors) == 1:
        return tensors[0]
    return tf.concat(tensors, axis=-1)


class WrapRNNCore(snt.RNNCore):
    """Wrap a transition function into an RNNCore."""

    def __init__(self, step, state_size, output_size, name=None):
        super(WrapRNNCore, self).__init__(name=name)
        self._step = step
        self._state_size = state_size
        self._output_size = output_size

    @property
    def output_size(self):
        """RNN output sizes."""
        return self._output_size

    @property
    def state_size(self):
        """RNN state sizes."""
        return self._state_size

    def _build(self, input_, state):
        return self._step(input_, state)


def add_support_for_scalar_rnn_inputs(cell, inputs):
    """Wraps a cell to add support for scalar RNN inputs."""
    flat_inputs = snt.nest.flatten(inputs)
    flat_input_is_scalar = [inp.get_shape().ndims == 2 for inp in flat_inputs]
    if not any(flat_input_is_scalar):
        return cell, inputs
    inputs = snt.nest.pack_sequence_as(
        inputs,
        [tf.expand_dims(inp, axis=-1) if is_scalar else inp
         for inp, is_scalar in zip(flat_inputs, flat_input_is_scalar)])
    is_scalar = snt.nest.pack_sequence_as(inputs, flat_input_is_scalar)

    def _squeeze(input_, is_scalar):
        return tf.squeeze(input_, axis=-1) if is_scalar else input_

    ret_cell = WrapRNNCore(
        lambda inp, state: cell(snt.nest.map(_squeeze, inp, is_scalar), state),
        state_size=cell.state_size,
        output_size=cell.output_size)
    return ret_cell, inputs


def input_recording_rnn(cell, input_size):
    """Transforms the cell to emit both the output and input."""
    def _step(input_, state):
        output, state = cell(input_, state)
        return (output, input_), state
    return WrapRNNCore(
        _step,
        state_size=cell.state_size,
        output_size=(cell.output_size, input_size))


def state_recording_rnn(cell):
    """Transforms the cell to emit both the output and the state."""
    def _step(input_, state):
        output, next_state = cell(input_, state)
        return (output, state), next_state
    return WrapRNNCore(
        _step,
        state_size=cell.state_size,
        output_size=(cell.output_size, cell.state_size))


def use_recorded_state_rnn(cell):
    """Transforms the cell to use the recorded state in the input."""
    def _step(input_state, state):
        del state  # unused
        input_, state = input_state
        return cell(input_, state)
    return WrapRNNCore(_step, cell.state_size, cell.output_size)


def heterogeneous_dynamic_rnn(
        cell, inputs, initial_state=None, time_major=False,
        output_dtypes=None, **kwargs):
    """Wrapper around tf.nn.dynamic_rnn that supports heterogeneous outputs."""
    time_axis = 0 if time_major else 1
    batch_axis = 1 if time_major else 0
    if initial_state is None:
        batch_size = batch_size_from_nested_tensors(inputs)
        initial_state = cell.zero_state(batch_size, output_dtypes)
    flat_dtypes = snt.nest.flatten(output_dtypes)
    flat_output_size = snt.nest.flatten(cell.output_size)
    # The first output will be returned the normal way; the rest will
    # be returned via state TensorArrays.
    input_length = sequence_size_from_nested_tensors(inputs)
    aux_output_tas = [
        tf.TensorArray(
            dtype,
            size=input_length,
            element_shape=tf.TensorShape([None]).concatenate(out_size))
        for dtype, out_size in zip(flat_dtypes[1:], flat_output_size[1:])
    ]
    aux_state = (0, aux_output_tas, initial_state)

    def _step(input_, aux_state):
        """Wrap the cell to return the first output and store the rest."""
        step, aux_output_tas, state = aux_state
        outputs, state = cell(input_, state)
        flat_outputs = snt.nest.flatten(outputs)
        aux_output_tas = [
            ta.write(step, output)
            for ta, output in zip(aux_output_tas, flat_outputs[1:])
        ]
        return flat_outputs[0], (step + 1, aux_output_tas, state)

    first_output, (_, aux_output_tas, state) = tf.nn.dynamic_rnn(
        WrapRNNCore(_step, state_size=None, output_size=flat_output_size[0]),
        inputs,
        initial_state=aux_state,
        dtype=flat_dtypes[0],
        time_major=time_major,
        **kwargs)
    first_output_shape = first_output.get_shape().with_rank_at_least(2)
    time_and_batch = tf.TensorShape([first_output_shape[time_axis],
                                     first_output_shape[batch_axis]])
    outputs = [first_output]
    for aux_output_ta in aux_output_tas:
        output = aux_output_ta.stack()
        output.set_shape(time_and_batch.concatenate(output.get_shape()[2:]))
        if not time_major:
            output = transpose_time_batch(output)
        outputs.append(output)
    return snt.nest.pack_sequence_as(output_dtypes, outputs), state


def transpose_time_batch(tensor):
    """Transposes the first two dimensions of a Tensor."""
    perm = list(range(tensor.get_shape().with_rank_at_least(2).ndims))
    perm[0], perm[1] = 1, 0
    return tf.transpose(tensor, perm=perm)


def reverse_dynamic_rnn(cell, inputs, time_major=False, **kwargs):
    """Runs tf.nn.dynamic_rnn backwards."""
    time_axis = 0 if time_major else 1
    reverse_seq = lambda x: tf.reverse(x, axis=[time_axis])
    inputs = snt.nest.map(reverse_seq, inputs)
    output, state = tf.nn.dynamic_rnn(
        cell, inputs, time_major=time_major, **kwargs)
    return snt.nest.map(reverse_seq, output), state


def dynamic_hparam(key, value):
    """Returns a memoized, non-constant Tensor that allows feeding."""
    collection = tf.get_collection_ref("HPARAMS_" + key)
    if len(collection) > 1:
        raise ValueError("Dynamic hparams ollection should contain one item.")
    if not collection:
        with tf.name_scope(""):
            default_value = tf.convert_to_tensor(value, name=key + "_default")
            tensor = tf.placeholder_with_default(
                default_value,
                default_value.get_shape(),
                name=key)
            collection.append(tensor)
    return collection[0]


def batch_size_from_nested_tensors(tensors):
    """Returns the batch dimension from the first non-scalar tensor given."""
    for tensor in snt.nest.flatten(tensors):
        if tensor.get_shape().ndims > 0:
            return tf.shape(tensor)[0]
    return None


def sequence_size_from_nested_tensors(tensors):
    """Returns the time dimension from the first K-tensor given where K > 1."""
    for tensor in snt.nest.flatten(tensors):
        if tensor.get_shape().ndims > 1:
            return tf.shape(tensor)[1]
    return None


def batch_size(hparams):
    """Returns a non-constant Tensor that evaluates to hparams.batch_size."""
    return dynamic_hparam("batch_size", hparams.batch_size)


def sequence_size(hparams):
    """Returns a non-constant Tensor that evaluates to hparams.sequence_size."""
    return dynamic_hparam("sequence_size", hparams.sequence_size)


def set_tensor_shapes(tensors, shapes, add_batch_dims=0):
    """Set static shape information for nested tuples of tensors and shapes."""
    if add_batch_dims:
        batch_dims = tf.TensorShape([None] * add_batch_dims)
        shapes = snt.nest.map(batch_dims.concatenate, shapes)
    snt.nest.map(lambda tensor, shape: tensor.set_shape(shape),
                 tensors, shapes)


def lazy_property(fn):
    """A property decorator that caches the returned value."""
    key = fn.__name__ + "_cache_val_"

    @functools.wraps(fn)
    def _lazy(self):
        """Very simple cache."""
        if not hasattr(self, key):
            setattr(self, key, fn(self))
        return getattr(self, key)

    return property(_lazy)
