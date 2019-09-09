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

# -*- coding: utf-8 -*-
"""Tests for dataset.py functionality."""

import io
import os.path
import tensorflow as tf

from vaeseq.examples.text import dataset as dataset_mod


class DatasetTest(tf.test.TestCase):

    def _write_corpus(self, text):
        """Save text to a temporary file and return the path."""
        temp_path = os.path.join(self.get_temp_dir(), "corpus.txt")
        with io.open(temp_path, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
        return temp_path

    def test_vocabulary(self):
        text = u"hello\nこんにちは"
        vocab_size = len(set(text))
        corpus = self._write_corpus(text)
        char_to_id, id_to_char = dataset_mod.vocabulary(corpus)
        ids = char_to_id.lookup(
            tf.constant([tf.compat.as_bytes(c)
                         for c in ["X", "l", "\n", u"こ"]]))
        chars = id_to_char.lookup(tf.constant([0, 100], dtype=tf.int64))
        with self.test_session() as sess:
            sess.run(tf.tables_initializer())
            ids, chars = sess.run([ids, chars])
            self.assertEqual(ids[0], vocab_size)
            self.assertTrue(0 <= ids[1] < vocab_size)
            self.assertTrue(0 <= ids[2] < vocab_size)
            self.assertTrue(0 <= ids[3] < vocab_size)
            chars = [tf.compat.as_text(c) for c in chars]
            self.assertTrue(chars[0] in text)
            self.assertEqual(chars[1], " ")

    def test_vocabulary_capped(self):
        text = u"hello\nこんにちは"
        corpus = self._write_corpus(text)
        char_to_id, id_to_char = dataset_mod.vocabulary(corpus, max_size=1,
                                                        num_oov_buckets=1)
        ids = char_to_id.lookup(
            tf.constant([tf.compat.as_bytes(c)
                         for c in ["X", "l", "\n", u"こ"]]))
        chars = id_to_char.lookup(tf.constant([0, 2], dtype=tf.int64))
        with self.test_session() as sess:
            sess.run(tf.tables_initializer())
            ids, chars = sess.run([ids, chars])
            self.assertAllEqual(ids, [1, 0, 1, 1])
            self.assertAllEqual(chars, [b"l", b" "])

    def test_characters(self):
        tf.set_random_seed(1)
        text = u"hello\nこんにちは"
        dataset = dataset_mod.characters(self._write_corpus(text), 2, 6)
        iterator = dataset.make_initializable_iterator()
        batch = iterator.get_next()
        with self.test_session() as sess:
            sess.run(iterator.initializer)
            self.assertAllEqual(
                sess.run(batch),
                [[tf.compat.as_bytes(c) for c in u"こんにちは\n"],
                 [tf.compat.as_bytes(c) for c in u"hello\n"]])


if __name__ == "__main__":
    tf.test.main()
