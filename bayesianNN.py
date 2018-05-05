from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# only for debugging purposes
import sys
import warnings
warnings.filterwarnings("ignore") # doesn't work?

# Dependencies
import matplotlib
matplotlib.use("Agg")
from matplotlib import figure  # pylint: disable=g-import-not-at-top
from matplotlib.backends import backend_agg
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
import json
import pandas as pd
from flags import *
from utils import *
from sklearn.decomposition import PCA
from sklearn.model_selection import KFold

# Test if seaborn is installed (for visualizations)
try:
  import seaborn as sns  # pylint: disable=g-import-not-at-top
  HAS_SEABORN = True
except ImportError:
  HAS_SEABORN = False

tfd = tf.contrib.distributions


# Import hyperparams from JSON file
with open('hyperparams.json') as json_data:
    hyperparams = json.load(json_data)
    json_data.close()

# Tuning program settings
FLAGS = flags.FLAGS
FLAGS.viz_epochs = 5000
FLAGS.viz_enabled = False # set to false if we want to train the model faster

train_percentage = 0.8

def plot_weight_posteriors(names, qm_vals, qs_vals, fname):
  """Save a PNG plot with histograms of weight means and stddevs.
  Args:
    names: A Python `iterable` of `str` variable names.
    qm_vals: A Python `iterable`, the same length as `names`,
      whose elements are Numpy `array`s, of any shape, containing
      posterior means of weight varibles.
    qs_vals: A Python `iterable`, the same length as `names`,
      whose elements are Numpy `array`s, of any shape, containing
      posterior standard deviations of weight varibles.
    fname: Python `str` filename to save the plot to.
  """
  fig = figure.Figure(figsize=(6, 3))
  canvas = backend_agg.FigureCanvasAgg(fig)

  ax = fig.add_subplot(1, 2, 1)
  for n, qm in zip(names, qm_vals):
    sns.distplot(qm.flatten(), ax=ax, label=n)
  ax.set_title("weight means")
  ax.set_xlim([-1.5, 1.5])
  ax.set_ylim([0, 4.])
  ax.legend()

  ax = fig.add_subplot(1, 2, 2)
  for n, qs in zip(names, qs_vals):
    sns.distplot(qs.flatten(), ax=ax)
  ax.set_title("weight stddevs")
  ax.set_xlim([0, 1.])
  ax.set_ylim([0, 25.])

  fig.tight_layout()
  canvas.print_figure(fname, format="png")
  print("saved {}".format(fname))

def plot_heldout_prediction(input_vals, probs,
                            fname, n=10, title=""):
  """Save a PNG plot visualizing posterior uncertainty on heldout data.
  Args:
    input_vals: A `float`-like Numpy `array` of shape
      `[num_heldout] + IMAGE_SHAPE`, containing heldout input images.
    probs: A `float`-like Numpy array of shape `[num_monte_carlo,
      num_heldout, num_classes]` containing Monte Carlo samples of
      class probabilities for each heldout sample.
    fname: Python `str` filename to save the plot to.
    n: Python `int` number of datapoints to vizualize.
    title: Python `str` title for the plot.
  """
  fig = figure.Figure(figsize=(9, 3*n))
  canvas = backend_agg.FigureCanvasAgg(fig)
  for i in range(n):
    ax = fig.add_subplot(n, 3, 3*i + 1)
    ax.imshow(input_vals[i, :].reshape(IMAGE_SHAPE), interpolation="None")

    ax = fig.add_subplot(n, 3, 3*i + 2)
    for prob_sample in probs:
      sns.barplot(np.arange(10), prob_sample[i, :], alpha=0.1, ax=ax)
      ax.set_ylim([0, 1])
    ax.set_title("posterior samples")

    ax = fig.add_subplot(n, 3, 3*i + 3)
    sns.barplot(np.arange(10), np.mean(probs[:, i, :], axis=0), ax=ax)
    ax.set_ylim([0, 1])
    ax.set_title("predictive probs")
  fig.suptitle(title)
  fig.tight_layout()

  canvas.print_figure(fname, format="png")
  print("saved {}".format(fname))


def build_input_pipeline(drug_data_path, batch_size,
                          number_of_principal_components):
  """Build an Iterator switching between train and heldout data.
  Args:
    `drug_data`: string representing the path to the .npy dataset.
    `batch_size`: integer specifying the batch_size for the dataset.
    `number_of_principal_components`: integer specifying how many principal components
    to reduce the dataset into.
  """
  # Build an iterator over training batches.
  with np.load(drug_data_path) as data:
    features = data["features"]
    labels = data["labels"]

    # PCA (sklearn) and normalising
    features = PCA(n_components=number_of_principal_components).fit_transform(features)

    # Splitting into training and validation sets
    train_range = int(train_percentage * len(features))

    training_features = features[:train_range]
    training_labels = labels[:train_range]
    validation_features = features[train_range:]
    validation_labels = labels[train_range:]

    # Z-normalising: (note with respect to training data)
    training_features = (training_features - np.mean(training_features, axis=0))/np.std(training_features, axis=0)
    validation_features = (validation_features - np.mean(training_features, axis=0))/np.std(training_features, axis=0)

  # Create the tf.Dataset object
  training_dataset = tf.data.Dataset.from_tensor_slices((training_features, training_labels))

  # Shuffle the dataset (note shuffle argument much larger than training size)
  # and form batches of size `batch_size`
  training_batches = training_dataset.shuffle(20000).repeat().batch(batch_size)
  training_iterator = training_batches.make_one_shot_iterator()

  # Build a iterator over the heldout set with batch_size=heldout_size,
  # i.e., return the entire heldout set as a constant.
  heldout_dataset = tf.data.Dataset.from_tensor_slices(
      (validation_features, validation_labels))
  heldout_frozen = (heldout_dataset.take(len(validation_features)).
                    repeat().batch(len(validation_features)))
  heldout_iterator = heldout_frozen.make_one_shot_iterator()

  # Combine these into a feedable iterator that can switch between training
  # and validation inputs.
  # Here should the minibatch increment be defined 
  handle = tf.placeholder(tf.string, shape=[])
  feedable_iterator = tf.data.Iterator.from_string_handle(
      handle, training_batches.output_types, training_batches.output_shapes)
  features_final, labels_final = feedable_iterator.get_next()

  return features_final, labels_final, handle, training_iterator, heldout_iterator, train_range


def main(argv):
  # extract the activation function from the hyperopt spec as an attribute from the tf.nn module
  activation = getattr(tf.nn, hyperparams['network_params']['activation_function'])

  # Tracking whether we are overwriting an old log directory or not
  if tf.gfile.Exists(FLAGS.model_dir):
    tf.logging.warning(
        "Warning: deleting old log directory at {}".format(FLAGS.model_dir))
    tf.gfile.DeleteRecursively(FLAGS.model_dir)
  tf.gfile.MakeDirs(FLAGS.model_dir)

  # define the graph
  with tf.Graph().as_default():
    # what's happening here?
    (features, labels, handle,
     training_iterator, heldout_iterator, train_range) = build_input_pipeline(
         "drug_data.npz", hyperparams['optimizer_params']['batch_size'],
         hyperparams['optimizer_params']['num_principal_components'])

    # Building the Bayesian Neural Network. 
    # We are here using the Gaussian Reparametrization Trick
    # to compute the stochastic gradients as described in the paper
    with tf.name_scope("bayesian_neural_net", values=[features]):
      neural_net = tf.keras.Sequential()
      for i in range(hyperparams['network_params']['num_hidden_layers']):
        layer = tfp.layers.DenseReparameterization(
            units=hyperparams['network_params']['num_neurons_per_layer'],
            activation=activation,
            trainable=True,
            kernel_prior_fn=default_multivariate_normal_fn, # NormalDiag with hyperopt sigma
            # kernel_prior_fn=make_scale_mixture_prior_fn, # pls work
            kernel_posterior_fn=tfp.layers.default_mean_field_normal_fn(), # softplus(sigma)
            kernel_posterior_tensor_fn=lambda x: x.sample(),
            bias_prior_fn=default_multivariate_normal_fn, # NormalDiag with hyperopt sigma
            bias_posterior_fn=tfp.layers.default_mean_field_normal_fn(), # softplus(sigma)
            bias_posterior_tensor_fn=lambda x: x.sample()
            )
        neural_net.add(layer)
      neural_net.add(tfp.layers.DenseReparameterization(
        units=1, # one dimensional output
        activation=None,
        trainable=True, # i.e subject to optimization
        kernel_prior_fn=default_multivariate_normal_fn, # NormalDiag with hyperopt sigma
        kernel_posterior_fn=tfp.layers.default_mean_field_normal_fn(), # softplus(sigma)
        kernel_posterior_tensor_fn=lambda x: x.sample(),
        bias_prior_fn=default_multivariate_normal_fn, # NormalDiag with hyperopt sigma
        bias_posterior_fn=tfp.layers.default_mean_field_normal_fn(), # softplus(sigma)
        bias_posterior_tensor_fn=lambda x: x.sample()
        ))
      predictions = neural_net(features)
      labels_distribution = tfd.Normal(loc=predictions, scale=[3.0]) # change hyperparam

      # Extract weight posterior statistics
      names = []
      qmeans = []
      qstds = []
      for i, layer in enumerate(neural_net.layers):
        q = layer.kernel_posterior
        q_b = layer.bias_posterior
        names.append("Layer {}".format(i))
        qmeans.append(q.mean())
        qmeans.append(q_b.mean())
        qstds.append(q.stddev())
        qstds.append(q_b.stddev())

      # weights_distribution = tfd.MultivariateNormalDiag(loc=qmeans, scale=qstds)

    # Compute (2^(M-i)/(2^M-1) where M is the mini-batch size, i current mini-batch index
    # i = tf.placeholder(tf.float32, name="i")
    # i = tf.Variable(1.0, tf.float32, name="i")
    # numerator = tf.pow(2.0, tf.subtract(11.0, i))
    # denominator = tf.subtract(tf.pow(2.0, 11.0), 1)
    # pi = tf.divide(numerator, denominator)

    #mprint(training_iterator._get_next_call_count)
    
    # Compute the -ELBO as the loss, averaged over the batch size.
    # neg_log_likelihood = -tf.reduce_mean(labels_distribution.log_prob(labels))
    neg_log_likelihood = tf.reduce_mean(tf.squared_difference(predictions, labels))
    # neg_log_likelihood = -tf.reduce_mean(tf.squared_difference(predictions, labels))
    kl = sum(neural_net.losses) / train_range
    elbo_loss = kl + neg_log_likelihood

    # Build metrics for evaluation. Predictions are formed from a single forward
    # pass of the probabilistic layers. They are cheap but noisy predictions.
    accuracy, accuracy_update_op = tf.metrics.mean_squared_error(
        labels=labels, predictions=predictions)

    with tf.name_scope("train"):
      # define optimizer - we are using (stochastic) gradient descent
      opt = tf.train.GradientDescentOptimizer(learning_rate=hyperparams['optimizer_params']['learning_rate'])

      # define that we want to minimize the loss (-ELBO)
      train_op = opt.minimize(elbo_loss)
      # start the session
      sess = tf.Session()
      # initialize the variables
      sess.run(tf.global_variables_initializer())
      sess.run(tf.local_variables_initializer())

      # Run the training loop
      train_handle = sess.run(training_iterator.string_handle())
      heldout_handle = sess.run(heldout_iterator.string_handle())

      # i = tf.Variable(0, name="batch_i")
      # increment_i = tf.assign_add(i, 1)
      
      # Run the epochs
      for epoch in range(hyperparams['optimizer_params']['epochs']):
        _ = sess.run([train_op, accuracy_update_op],
                     feed_dict={handle: train_handle})

        if epoch % 100 == 0:
          loss_value, accuracy_value = sess.run(
            [elbo_loss, accuracy], feed_dict={handle: train_handle})
          loss_value_validation, accuracy_value_validation = sess.run(
            [elbo_loss, accuracy], feed_dict={handle: heldout_handle}
          )
          print("Epoch: {:>3d} Loss: [{:.3f}, {:.3f}] Accuracy: [{:.3f}, {:.3f}]".format(
              epoch, loss_value, loss_value_validation, accuracy_value, accuracy_value_validation))

        # check if time to save vizualisations
        '''if (epoch+1) % FLAGS.viz_epochs == 0 & False:
          # Compute log prob of heldout set by averaging draws from the model:
          # p(heldout | train) = int_model p(heldout|model) p(model|train)
          #                   ~= 1/n * sum_{i=1}^n p(heldout | model_i)
          # where model_i is a draw from the posterior p(model|train).
          probs = np.asarray([sess.run((labels_distribution.probs),
                                       feed_dict={handle: heldout_handle}) # will need to change to multivariate normal
                              for _ in range(FLAGS.num_monte_carlo)])
          mean_probs = np.mean(probs, axis=0)

          image_vals, label_vals = sess.run((features, labels),
                                            feed_dict={handle: heldout_handle}) #?
          heldout_lp = np.mean(np.log(mean_probs[np.arange(mean_probs.shape[0]),
                                                 label_vals.flatten()])) #?
          print(" ... Held-out nats: {:.3f}".format(heldout_lp))

          qm_vals, qs_vals = sess.run((qmeans, qstds)) # variational posterior parameters

          if HAS_SEABORN & FLAGS.viz_enabled: 
            plot_weight_posteriors(names, qm_vals, qs_vals,
                                   fname=os.path.join(
                                       FLAGS.model_dir,
                                       "epoch{:05d}_weights.png".format(epoch)))

            plot_heldout_prediction(image_vals, probs,
                                    fname=os.path.join(
                                        FLAGS.model_dir,
                                        "epoch{:05d}_pred.png".format(epoch)),
                                    title="mean heldout logprob {:.2f}"
                                    .format(heldout_lp))'''

if __name__ == "__main__":
  tf.app.run()