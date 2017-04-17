from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import threading

import tensorflow as tf

import data as shake_data
import model as shake_model

flags = tf.flags

flags.DEFINE_string("model_dir", "./tmp", (
    "Model directory."))
flags.DEFINE_string("source_train", shake_data.default_modern_train_path, (
    ""))
flags.DEFINE_string("source_dev", shake_data.default_modern_dev_path, (
    ""))
flags.DEFINE_string("target_train", shake_data.default_original_train_path, (
    ""))
flags.DEFINE_string("target_dev", shake_data.default_original_dev_path, (
    ""))

flags.DEFINE_integer("logging_frequency", 100, (
    ""
))

flags.DEFINE_integer("batch_size", 32, (
    ""))

flags.DEFINE_string("optimizer_type", "adam", (
    ""))
flags.DEFINE_float("learning_rate", 1e-4, (
    ""))
flags.DEFINE_float("max_grads", 5.0, (
    ""))

flags.DEFINE_integer("num_rnn_layers", 1, (
    ""
))
flags.DEFINE_boolean("is_bidi", False, (
    ""
))
flags.DEFINE_boolean("reverse_sequence", False, (
    ""
))
flags.DEFINE_boolean("add_attention", False, (
    ""
))
# flags.DEFINE_boolean("tied_weights", False, (
#     ""
# ))


FLAGS = flags.FLAGS


# TODO: randomize the data
def get_input_queues(source_path, source_vocab, target_path, target_vocab,
                     batch_size=32, num_threads=8, maxlen=None):

    source_ph = tf.placeholder(tf.int32, shape=[None, maxlen])  # [B, T]
    target_ph = tf.placeholder(tf.int32, shape=[None, maxlen])  # [B, T]

    queue = tf.PaddingFIFOQueue(shapes=[[maxlen, ], [maxlen, ]], dtypes=[tf.int32, tf.int32], capacity=5000,)

    enqueue_op = queue.enqueue_many([source_ph, target_ph])
    def enqueue_data(sess):
        epoch = 0
        while True:
            tf.logging.info("Epoch %i", epoch)
            for source, target in shake_data.data_iterator(
                    source_path, source_vocab, target_path, target_vocab, batch_size=batch_size):
                sess.run(enqueue_op, feed_dict={source_ph: source, target_ph: target})
            epoch += 1

    dequeue_op = queue.dequeue_many(batch_size)
    dequeue_batch = dequeue_op
    # dequeue_batch = tf.train.batch([dequeue_op], batch_size=batch_size, num_threads=num_threads, capacity=1000,
    #                                dynamic_pad=True, enqueue_many=True, name="batch_and_pad")

    return enqueue_data, dequeue_batch


def start_threads(thread_fn, args, n_threads=1):
    assert n_threads == 1, "Having multiple threads causes duplicate data in the queue."

    threads = []
    for n in range(n_threads):
        t = threading.Thread(target=thread_fn, args=args)
        t.daemon = True  # thread will close when parent quits
        t.start()
        threads.append(t)

    time.sleep(1)  # enqueue a bunch before dequeue
    return threads


def set_train_op(loss, tvars):
    # TODO: initializer all optimizers? so can be used later
    if FLAGS.optimizer_type == "sgd":
        optimizer = tf.train.GradientDescentOptimizer(learning_rate=FLAGS.learning_rate)
    elif FLAGS.optimizer_type == "rmsprop":
        optimizer = tf.train.RMSPropOptimizer(learning_rate=FLAGS.learning_rate)
    elif FLAGS.optimizer_type == "adam":
        optimizer = tf.train.AdamOptimizer(learning_rate=FLAGS.learning_rate)
    else:
        raise ValueError("Wrong optimizer_type.")

    gradients = optimizer.compute_gradients(loss, var_list=tvars)
    clipped_gradients = [(grad if grad is None else tf.clip_by_norm(grad, FLAGS.max_grads), var)
                         for grad, var in gradients]

    train_op = optimizer.apply_gradients(clipped_gradients)
    return train_op


def _sample_text(sess, sampled_word_ids, input_ph, text, vocab, rev_vocab):
    tokens = shake_data.tokenize(text)
    vector = shake_data.vectorize(tokens, vocab)
    generated = sess.run(sampled_word_ids, feed_dict={input_ph: [vector]})
    text = " ".join([rev_vocab[i] for i in generated.tolist()[0]])
    return text


def main(_argv):
    source_word2idx, source_idx2word = shake_data.build_vocab(FLAGS.source_train)
    target_word2idx, target_idx2word = shake_data.build_vocab(FLAGS.target_train)

    source_vocab_size = len(source_word2idx)
    target_vocab_size = len(target_word2idx)

    tf.logging.info("source vocab size: %i target vocab size: %i", source_vocab_size, target_vocab_size)

    encoder = shake_model.Encoder(source_vocab_size,
                                  num_rnn_layers=FLAGS.num_rnn_layers,
                                  is_bidi=FLAGS.is_bidi,
                                  reverse_sequence=FLAGS.reverse_sequence)
    decoder = shake_model.Decoder(target_vocab_size,
                                  add_attention=FLAGS.add_attention)
    seq2seq_model = shake_model.Seq2Seq(encoder, decoder)

    enqueue_data, dequeue_batch = get_input_queues(
        FLAGS.source_train, source_word2idx, FLAGS.target_train, target_word2idx,
        batch_size=FLAGS.batch_size)

    decoder_outputs, decoder_targets = seq2seq_model(*dequeue_batch)
    cost = seq2seq_model.cost(decoder_outputs, decoder_targets)

    # TODO: add valid cost

    input_ph = tf.placeholder(tf.int32, shape=(None, None))
    decoder_outputs_inference, _ = seq2seq_model(input_ph)
    sampled_word_ids = seq2seq_model.generate(decoder_outputs_inference)

    tvars = tf.trainable_variables()
    train_op = set_train_op(cost, tvars)

    global_step = seq2seq_model._increment_global_step
    increment = seq2seq_model._global_step

    saver = tf.train.Saver()

    hooks = [
        # TODO: enqueue operation errors out using this hook
        # tf.train.LoggingTensorHook({
        #     "cost": cost,
        # }, every_n_secs=60),
        tf.train.StepCounterHook(every_n_steps=100),
        tf.train.CheckpointSaverHook(
            checkpoint_dir=FLAGS.model_dir,
            save_steps=100,
            saver=saver)
    ]

    scaffold = tf.train.Scaffold(
        init_op=None,
        init_feed_dict=None,
        init_fn=None,
        ready_op=None,
        ready_for_local_init_op=None,
        local_init_op=None,
        summary_op=None,
        saver=saver)

    with tf.train.SingularMonitoredSession(hooks=hooks, checkpoint_dir=FLAGS.model_dir, scaffold=scaffold) as sess:
        start_threads(enqueue_data, (sess, ), n_threads=1)

        while True:
            _, c, _, step = sess.run(
                [train_op, cost, increment, global_step])

            if step % FLAGS.logging_frequency == 0:
                tf.logging.info("Global step: %i", step)
                tf.logging.info("Cost: %.4f", c)
                text = "have you killed Tybalt?"
                tf.logging.info(_sample_text(sess, sampled_word_ids, input_ph, text, source_word2idx, target_idx2word))


if __name__ == "__main__":
    tf.logging.set_verbosity(tf.logging.INFO)
    tf.app.run()
