__author__ = 'Guillaume'

import numpy as np
import platform
import os
import pickle
import time
import sys

from contextlib import contextmanager
from keras.callbacks import Callback, EarlyStopping
import keras.backend as K
from preprocessing import resize_pil, check_preprocessed_data, convert_labels, standardize_dataset, preprocess_dataset, \
    get_next_batch
from reporting import write_experiment_report, print_architecture
from training_params import TrainingParams
from dataset import InMemoryDataset, FuelDataset
from testing import get_best_model_from_exp, test_model, update_BN_params, adapt_to_new_input, categorical_crossentropy

def save_history(path, history):
    """
    Save the loss, validation loss, accuracy and validation accuracy of a Keras training into a pickle file.

    :param path: where to save the pickle file
    :param history: an History object returned by the fit function of Keras
    :return:
    """
    with open(path,"w") as f:
        pickle.dump(history.history["loss"],f)
        pickle.dump(history.history["val_loss"],f)
        pickle.dump(history.history["acc"],f)
        pickle.dump(history.history["val_acc"],f)

class ModelCheckpoint_perso(Callback):
    """
    Keras callback subclass which defines a saving procedure of the model being trained : after each epoch,
    the last model is saved under the name 'after_random.cnn'. The best model is saved with the name 'best_model.cnn'.
    The model after random can also be saved. And the model architecture is saved with the name 'config.network'.
    Everything is stored using pickle.
    """
    def __init__(self, filepath, monitor='val_acc', verbose=1, save_best_only=False, save_first=True, optional_string="",
                 mode="acc"):
        super(Callback, self).__init__()
        self.monitor = monitor
        self.verbose = verbose
        self.filepath = filepath
        self.save_best_only = save_best_only
        self.save_first = save_first
        self.optional_string = optional_string
        self.mode = mode
        if mode == "acc":
            self.best = -np.Inf
        elif mode == "loss":
            self.best = np.Inf
        else:
            print "Mode not undestood. It should be 'loss' or 'acc'."


    def on_epoch_begin(self, epoch, logs={}):
        if epoch==0:
            if os.path.exists(self.filepath) is False:
                os.mkdir(self.filepath)

            open(self.filepath+"/config.netconf", 'w').write(self.model.to_json())

            save_path = self.filepath+"/after_random.cnn"
            if self.verbose > 0:
                f = open(self.filepath+"/log.txt", "w")
                f.write(self.optional_string)
                f.write("***\nEpoch %05d: %s after random  model saved to %s\n"%(epoch, self.monitor, save_path))
                f.close()
            if self.save_first:
                self.model.save_weights(save_path, overwrite=True)

    def on_epoch_end(self, epoch, logs={}):
        # SAVING WEIGHTS
        current = logs.get(self.monitor)
        if self.mode=="acc":
            condition = current > self.best
        else:
            condition = current < self.best
        if condition:
            save_path = self.filepath+"/best_model.cnn"
            if self.verbose > 0:
                string = "***\nEpoch %05d: %s improved from %0.5f to %0.5f\n"% (epoch, self.monitor, self.best, current)
                write_log(self.filepath+"/log.txt", string)
            self.best = current
            self.model.save_weights(save_path, overwrite=True)

        else:
            save_path = self.filepath+"/last_epoch.cnn"
            if self.verbose > 0:
                string = "***\nEpoch %05d: %s did not improve : %0.5f\n"% (epoch, self.monitor, current)
                write_log(self.filepath+"/log.txt", string)
            self.model.save_weights(save_path, overwrite=True)

def write_log(path, string):
    """
    Add a line at the end of a textfile.

    :param path: textfile location
    :param string: line to add
    """
    # Open and Read
    f = open(path, "r")
    lines = f.readlines()
    f.close()
    # Adding a line
    lines.append(string)
    # Write
    f = open(path, "w")
    f.writelines(lines)
    f.close()

@contextmanager
def timer(name):
    """
    Print the time taken by some operations. Usage :
    with timer("Operation A"):
        operation

    :param name: just a string name for the print function
    :return:
    """
    start_time = time.time()
    yield
    stop_time = time.time()
    print('\n{} took {} seconds'.format(name, stop_time - start_time))

def load_dataset_in_memory_and_resize(data_access, set, dataset_path, targets_path, tmp_size, final_size, batch_size):
    if data_access == "in-memory":
        with timer("Loading %s data"%set):
            dataset = InMemoryDataset(set, dataset_path, source_targets=targets_path)
            draw_data = np.copy(dataset.dataset)
            targets = np.copy(dataset.targets)
            del dataset
    elif data_access == "fuel":
        with timer("Loading %s data"%set):
            dataset = FuelDataset(set, tmp_size, batch_size=batch_size, shuffle=False)
            draw_data,targets = dataset.return_whole_dataset()
            del dataset
    else:
        raise Exception("Data access not available. Must be 'fuel' or 'in-memory'. Here : %s."%data_access)

    if tmp_size != final_size:
        # Resize images from the validset
        out = np.zeros((draw_data.shape[0], final_size[0], final_size[1], final_size[2]), dtype="float32")
        with timer("Resizing %s images"%set):
            for i in range(draw_data.shape[0]):
                out[i] = resize_pil(draw_data[i], final_size[0:2])
        del draw_data
        return out, targets
    else:
        return draw_data, targets

def launch_training(training_params):
    """
    Load the data, and train a Keras model.

    :param training_params: a TrainingParams object which contains each parameter of the training
    :return:
    """
    if os.path.exists(training_params.path_out) is False:
        os.mkdir(os.path.abspath(training_params.path_out))

    ###### LOADING DATA #######
    validset, valid_targets = load_dataset_in_memory_and_resize(training_params.data_access, "valid", training_params.dataset_path,
                                                                training_params.targets_path, training_params.final_size,
                                                                training_params.final_size, training_params.test_batch_size)
    valid_targets = convert_labels(valid_targets)

    ###### Preprocessing VALIDATION DATA #######
    for mode in training_params.valid_preprocessing:
        validset = preprocess_dataset(validset, training_params, mode)
    # Transpose validset >> (N, channel, X, Y)
    validset = validset.transpose(0,3,1,2)
    # Multiple input ?
    if training_params.multiple_inputs>1:
        validset = [validset for i in range(training_params.multiple_inputs)]

    ###### MODEL INITIALIZATION #######
    with timer("Model initialization"):
        model = training_params.initialize_model()
    if training_params.pretrained_model is not None:
        with timer("Pretrained Model initialization"):
            pretrained_model = training_params.initialize_pretrained_model()
            training_params.generator_args.append(pretrained_model)
            # preprocessed the validset
            if type(pretrained_model) is list:
                features = []
                for pmodel in pretrained_model:
                    features.append(pmodel.predict(validset))
                validset = np.concatenate(features, axis=1)
            else:
                validset = pretrained_model.predict(validset)

    ###### SAVE PARAMS ######
    s = training_params.print_params()
    # Save command
    f = open(training_params.path_out+"/command.txt", "w")
    f.writelines(" ".join(sys.argv))
    f.writelines(s)
    f.close()
    # Print architecture
    print_architecture(model, path_out=training_params.path_out + "/architecture.txt")

    ###### TRAINING LOOP #######
    count = training_params.fine_tuning

    with timer("Training"):
        while training_params.learning_rate >= training_params.learning_rate_min and count<training_params.nb_max_epoch:

            if count != 0: # Restart from the best model with a lower LR
                model = training_params.initialize_model()
                model.load_weights(training_params.path_out+"/MEM_%d/best_model.cnn"%(count-1))
            # Callbacks
            early_stoping = EarlyStopping(monitor="val_loss",patience=training_params.max_no_best)
            save_model = ModelCheckpoint_perso(filepath=training_params.path_out+"/MEM_%d"%count, verbose=1,
                                               optional_string=s, monitor="val_acc", mode="acc")

            history = model.fit_generator(training_params.generator(*training_params.generator_args),
                                          nb_epoch=training_params.nb_max_epoch,
                                          samples_per_epoch= int(training_params.Ntrain*training_params.bagging_size)/5,
                                          show_accuracy=True,
                                          verbose=training_params.verbose,
                                          validation_data=(validset,  valid_targets),
                                          callbacks=[early_stoping, save_model])

            training_params.learning_rate *= 0.1
            training_params.update_model_args()
            save_history(training_params.path_out+"/MEM_%d/history.pkl"%count, history)
            count += 1

def test_model_on_exp(training_params, testset=None, labels=None, verbose=False, write_txt_file=False,
                      return_testset=False):
    # Get the best model
    model, path_model = get_best_model_from_exp(training_params.path_out)
    initial_input_shape = model.input_shape
    print "\n" + path_model
    k = 0
    lines = []
    for test_size in training_params.test_sizes:
        if verbose:
            s = "\nTesting for size :" + str(test_size)
            print s
            lines.append(s)
        # Get the best model
        if test_size[0] != model.input_shape[2] or test_size[1] != model.input_shape[3]:
            new_model = adapt_to_new_input(model, (test_size[2],test_size[0],test_size[1]), initial_input_shape[1:],
                                           verbose=True)
        else:
            new_model = model
        # if testset is None or labels is None:
        #     # If not given, get the testset
        #     testset, testset_labels = load_dataset_in_memory_and_resize(training_params.data_access, "test",
        #                                                                 training_params.dataset_path,
        #                                                                 training_params.targets_path,
        #                                                                 test_size,
        #                                                                 test_size,
        #                                                                 training_params.batch_size)
        #     labels = convert_labels(testset_labels)
        # if testset.shape[2:4]!=test_size[0:2]:
        #     new_testset = np.zeros((testset.shape[0], test_size[2], test_size[0], test_size[1]), dtype="float32")
        #     with timer("Resizing %s images"%set):
        #         for i in range(testset.shape[0]):
        #             new_testset[i] = resize_pil(testset[i], testset[0:2]).transpose(2,0,1)
        #     testset = np.copy(new_testset)
        #     del new_testset
        testset = FuelDataset("test", test_size, batch_size=training_params.test_batch_size, shuffle=False)
        # # Input normalization
        # if training_params.valid_preprocessing == "scale":
        #     testset = testset / training_params.scale
        # if training_params.valid_preprocessing == "std":
        #     testset = standardize_dataset(testset, [1,2,3])
        # Predictions on the draw testset
        score, loss, preds, labels  = test_model(new_model, testset, training_params,
                                                 flip=False, verbose=verbose, return_preds=True)
        if write_txt_file:
            lines.append("\n\tDraw testset score = %.5f\n\tDraw testset loss = %.5f"%(score,loss))
        if k == 0:
            final_preds = np.copy(preds)
        else:
            final_preds += preds
        k+=1.0
        # Predictions on the flipped testset
        flipped_score, flipped_loss, flipped_preds, labels = test_model(new_model, testset, training_params,
                                                                        flip=True, verbose=verbose, return_preds=True)
        if write_txt_file:
            lines.append("\n\tFlipped testset score = %.5f\n\tFlipped testset loss = %.5f"%(flipped_score,flipped_loss))
        final_preds += flipped_preds
        k+=1.0

    # Arithmetic averaging of predictions
    final_preds_arithm = final_preds/k
    count = np.sum(np.argmax(labels, axis=1) - np.argmax(final_preds_arithm, axis=1) == 0)
    final_score_arithm = float(count)/labels.shape[0]
    if verbose:
        s = "\nFinal score (arithm) =%.5f"%final_score_arithm
        print s
        lines.append(s)
    # Geometric fusion
    # final_preds_geom = np.sqrt(preds*flipped_preds)
    # count = np.sum(np.argmax(labels, axis=1) - np.argmax(final_preds_geom, axis=1) == 0)
    # final_score_geom = float(count)/labels.shape[0]
    # if verbose:
    #     print "Final score (geom) =%.5f"%final_score_geom

    if write_txt_file:
        f = open(training_params.path_out+"/testset_score.txt", "w")
        for line in lines:
            f.writelines(line)
        f.close()

    return final_preds_arithm, final_score_arithm, labels


def test_ensemble_of_models(training_params, path_out=os.path.abspath("experiments/ensemble_of_models.txt"),
                            write_txt=True, verbose=True):
    predictions = []
    scores = []
    for i,path in enumerate(training_params.ensemble_models):
        # For each model, get the predictions
        training_params.path_out = path
        # Get predictions, No need to get the testset
        model_preds, model_score, labels = test_model_on_exp(training_params,
                                                             verbose=verbose, write_txt_file=False)
        training_params.test_sizes = [(270,270,3), (210,210,3)]
        # Accumulate predictions and scores
        predictions.append(model_preds)
        scores.append(model_score)
        if verbose:
            print "%s = %.5f"%(path, model_score)
    # Fusion
    final_predictions = np.mean(np.array(predictions), axis=0)
    count = np.sum(np.argmax(labels, axis=1) - np.argmax(final_predictions, axis=1) == 0)
    final_score = float(count)/labels.shape[0]
    if verbose:
        print "Ensemble Score = %.5f\n"%(final_score)
    # Write the result in a textfile
    if write_txt:
        f = open(path_out, "w")
        for i,path in enumerate(training_params.ensemble_models):
            f.writelines("%s = %.5f\n"%(path,scores[i]))
        f.writelines("Ensemble Score = %.5f\n"%(final_score))
        f.close()


if __name__ == "__main__":
    end = False
    mode = ""
    try:
        mode = sys.argv[1]
    except:
        print "Expects an argument : '-train', '-check', '-report', '-test' or 'ensemble'."
        end = True

    if end is not True:
        training_params = TrainingParams()
        if mode=="-train":
            for i in range(training_params.multiple_training):
                launch_training(training_params)
                test_model_on_exp(training_params, verbose=True, write_txt_file=True)
                if platform.system()=="Windows":
                    write_experiment_report(training_params.path_out, multipages=True)
                    write_experiment_report(training_params.path_out, multipages=False)
                training_params.update_params_for_next_training()
        if mode=="-adversarial":
            for i in range(training_params.multiple_training):
                launch_adversarial_training(training_params)
                test_model_on_exp(training_params, verbose=True, write_txt_file=True)
                if platform.system()=="Windows":
                    write_experiment_report(training_params.path_out, multipages=True)
                    write_experiment_report(training_params.path_out, multipages=False)
                training_params.update_params_for_next_training()
        elif mode=="-check":
            try:
                n = int(sys.argv[2])
            except:
                n=10
            check_preprocessed_data(training_params.data_access,
                                    training_params.dataset_path,
                                    training_params.targets_path,
                                    training_params.batch_size,
                                    training_params.tmp_size,
                                    training_params.final_size,
                                    training_params.preprocessing_func,
                                    training_params.preprocessing_args,
                                    n=n)
        elif mode=="-report":
            write_experiment_report(training_params.path_out, multipages=True)
            write_experiment_report(training_params.path_out, multipages=False)
        elif mode=="-test":
            test_model_on_exp(training_params, verbose=True, write_txt_file=True)
        elif mode=="-ensemble":
            test_ensemble_of_models(training_params)
        else:
            print "Mode not undertstood. Use '-train', '-check', '-report', or '-test'. Here : %s"%mode
