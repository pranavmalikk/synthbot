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

"""The model for MIDI music.

At each time step, we predict a pair of:
* 128 independent Beta variables, assigning scores to each note.
* K in [0,10], a Categorical variable counting the number of notes played.

When generating music, we emit the top K notes per timestep.
"""

from __future__ import print_function

import numpy as np
import tensorflow as tf

from vaeseq import codec as codec_mod
from vaeseq import context as context_mod
from vaeseq import model as model_mod
from vaeseq import util

from . import dataset as dataset_mod


class Model(model_mod.ModelBase):
    """Putting everything together."""

    def _make_encoder(self):
        """Constructs an encoder for a single observation."""
        return codec_mod.MLPObsEncoder(self.hparams, name="obs_encoder")

    def _make_decoder(self):
        """Constructs a decoder for a single observation."""
        # We need 2 * 128 (note beta) + 11 (count categorical) parameters.
        params = util.make_mlp(
            self.hparams,
            self.hparams.obs_decoder_fc_hidden_layers + [128 * 2 + 11])
        def _split_params(inp):
            note_params, count_param = tf.split(inp, [128 * 2, 11], axis=-1)
            return (note_params, count_param)  # Note: returning a tuple.
        single_note_decoder = codec_mod.BetaDecoder(
            positive_projection=util.positive_projection(self.hparams))
        notes_decoder = codec_mod.BatchDecoder(
            single_note_decoder, event_size=[128], name="notes_decoder")
        count_decoder = codec_mod.CategoricalDecoder(name="count_decoder")
        full_decoder = codec_mod.GroupDecoder((notes_decoder, count_decoder))
        return codec_mod.DecoderSequence(
            [params, _split_params], full_decoder, name="decoder")

    def _make_feedback(self):
        """Constructs the feedback Context."""
        history_combiner = codec_mod.EncoderSequence(
            [codec_mod.FlattenEncoder(),
             util.make_mlp(self.hparams,
                           self.hparams.history_encoder_fc_layers)],
            name="history_combiner"
        )
        return context_mod.Accumulate(
            obs_encoder=self.encoder,
            history_size=self.hparams.history_size,
            history_combiner=history_combiner)

    def _make_dataset(self, files):
        dataset = dataset_mod.piano_roll_sequences(
            files,
            util.batch_size(self.hparams),
            util.sequence_size(self.hparams),
            rate=self.hparams.rate)
        iterator = dataset.make_initializable_iterator()
        tf.add_to_collection(tf.GraphKeys.LOCAL_INIT_OP, iterator.initializer)
        piano_roll = iterator.get_next()
        shape = tf.shape(piano_roll)
        notes = tf.where(piano_roll, tf.fill(shape, 0.95), tf.fill(shape, 0.05))
        counts = tf.minimum(10, tf.reduce_sum(tf.to_int32(piano_roll), axis=-1))
        observed = (notes, counts)
        inputs = None
        return inputs, observed

    # Samples per second when generating audio output.
    SYNTHESIZED_RATE = 16000
    def _render(self, observed):
        """Returns a batch of wave forms corresponding to the observations."""
        notes, counts = observed

        def _synthesize(notes, counts):
            """Use pretty_midi to synthesize a wave form."""
            piano_roll = np.zeros((len(counts), 128), dtype=np.bool)
            top_notes = np.argsort(notes)
            for roll_t, top_notes_t, k in zip(piano_roll, top_notes, counts):
                if k > 0:
                    for i in top_notes_t[-k:]:
                        roll_t[i] = True
            rate = self.hparams.rate
            midi = dataset_mod.piano_roll_to_midi(piano_roll, rate)
            wave = midi.synthesize(self.SYNTHESIZED_RATE)
            wave_len = len(wave)
            expect_len = (len(piano_roll) * self.SYNTHESIZED_RATE) // rate
            if wave_len < expect_len:
                wave = np.pad(wave, [0, expect_len - wave_len], mode='constant')
            else:
                wave = wave[:expect_len]
            return np.float32(wave)

        # Apply synthesize_roll on all elements of the batch.
        def _map_batch_elem(notes_counts):
            notes, counts = notes_counts
            return tf.py_func(_synthesize, [notes, counts], [tf.float32])[0]
        return tf.map_fn(_map_batch_elem, (notes, counts), dtype=tf.float32)

    def _make_output_summary(self, tag, observed):
        notes, counts = observed
        return tf.summary.merge(
            [tf.summary.audio(
                tag + "/audio",
                self._render(observed),
                self.SYNTHESIZED_RATE,
                collections=[]),
             tf.summary.scalar(
                 tag + "/note_avg",
                 tf.reduce_mean(notes)),
             tf.summary.scalar(
                 tag + "/note_count",
                 tf.reduce_mean(tf.to_float(counts)))])
