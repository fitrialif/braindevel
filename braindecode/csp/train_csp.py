from wyrm.processing import select_channels, append_cnt
from braindecode.mywyrm.processing import (
    resample_cnt, highpass_cnt, common_average_reference_cnt)
from braindecode.mywyrm.clean import (
    compute_rejected_channels_trials_cnt, BBCITwoSetsCleaner)
import itertools
from sklearn.cross_validation import KFold
from braindecode.csp.pipeline import (BinaryCSP, FilterbankCSP,
    MultiClassWeightedVoting)
from braindecode.datasets.bbci_dataset import BBCIDataset
import numpy as np
from copy import deepcopy
from pylearn2.utils import serial
from braindecode.datasets.sensor_positions import sort_topologically
import logging
log = logging.getLogger(__name__)

class CSPTrain(object):
    def __init__(self,filename, sensor_names=None, load_sensor_names=None,
            low_cut_off_hz=0.5, rejection_var_ival=[0,4000], 
            rejection_blink_ival=[-500,4000],
            max_min=600, whisker_percent=10, whisker_length=3,
            resample_fs=300,
            min_freq=0, max_freq=48, last_low_freq=48,
            low_width=4, high_width=4,
            filt_order=3,
            segment_ival=[500,4000], num_folds=10,
            num_filters=None, num_selected_filterbands=None,
            num_selected_features=None,
            forward_steps=2,
            backward_steps=1,
            stop_when_no_improvement=False,
            only_last_fold=False,
            restricted_n_trials=None,
            common_average_reference=False,
            ival_optimizer=None):
        local_vars = locals()
        del local_vars['self']
        self.__dict__.update(local_vars)
        # remember params for later result printing etc
        self.original_params = deepcopy(local_vars)

    def get_trainer(self):
        """ just for later saving"""
        return self

    def run(self):
        log.info("Loading set...")
        self.load_bbci_set()
        log.info("Cleaning set...")
        self.clean_set()
        log.info("Preprocessing set...")
        self.preprocess_set()
        self.remember_sensor_names()
        self.init_training_vars()
        log.info("Running Training...")
        self.run_training()

    def load_bbci_set(self):
        #TODELAY: do we even need to assign bbci_set to this object?
        self.bbci_set = BBCIDataset(self.filename, 
            sensor_names=self.load_sensor_names)
        self.bbci_set.load_continuous_signal()
        self.bbci_set.add_markers()
        self.cnt = self.bbci_set.cnt
        del self.bbci_set.cnt # save memory

    def clean_set(self):
        (rejected_chans, rejected_trials, reject_max_min, reject_var, 
            clean_trials) = compute_rejected_channels_trials_cnt(self.cnt, 
                filename=self.filename, 
                rejection_blink_ival=self.rejection_blink_ival, 
                max_min=self.max_min, 
                rejection_var_ival=self.rejection_var_ival, 
                whisker_percent=self.whisker_percent, 
                whisker_length=self.whisker_length, 
                low_cut_hz=0.5, 
                high_cut_hz=150,
                filt_order=4)
        self.rejected_chans = rejected_chans
        self.rejected_trials = rejected_trials
        self.rejected_trials_max_min = reject_max_min
        self.rejected_trials_var = reject_var
        self.clean_trials = clean_trials
        # do not remove rejected channels yet to allow calling
        # this function several times with same result

    def preprocess_set(self):
        # only remove rejected channels now so that clean function can
        # be called multiple times without changing cleaning results
        self.cnt = select_channels(self.cnt, self.rejected_chans, invert=True)
        if self.sensor_names is not None:
            self.sensor_names = sort_topologically(self.sensor_names)
            self.cnt = select_channels(self.cnt, self.sensor_names)

        self.cnt = resample_cnt(self.cnt, newfs=self.resample_fs)
        if self.low_cut_off_hz is not None:
            self.cnt = highpass_cnt(self.cnt, low_cut_off_hz=self.low_cut_off_hz)
        if self.common_average_reference is True:
            self.cnt = common_average_reference_cnt(self.cnt)

    def remember_sensor_names(self):
        """ Just to be certain have correct sensor names, take them
        from cnt signal"""
        self.sensor_names = self.cnt.axes[1]

    def init_training_vars(self):
        self.filterbands = generate_filterbank(min_freq=self.min_freq,
            max_freq=self.max_freq, last_low_freq=self.last_low_freq, 
            low_width=self.low_width, high_width=self.high_width)
        self.class_pairs = list(itertools.combinations([0,1,2,3],2))
        # use only number of clean trials to split folds
        num_clean_trials = len(self.clean_trials)
        if self.restricted_n_trials is not None:
            num_clean_trials = int(num_clean_trials * self.restricted_n_trials)
        folds = KFold(num_clean_trials, n_folds=self.num_folds, 
            shuffle=False)
        # remap to original indices in unclean set(!)
        self.folds = map(lambda fold: 
            {'train': self.clean_trials[fold[0]], 
             'test': self.clean_trials[fold[1]]},
            folds)
        if self.only_last_fold:
            self.folds = self.folds[-1:]

    def run_training(self):
        self.binary_csp = BinaryCSP(self.cnt, self.filterbands, 
            self.filt_order, self.folds, self.class_pairs, 
            self.segment_ival, self.num_filters, 
            ival_optimizer=self.ival_optimizer)
        self.binary_csp.run()
        
        self.filterbank_csp = FilterbankCSP(self.binary_csp, 
            num_features=self.num_selected_features,
            num_filterbands=self.num_selected_filterbands,
            forward_steps=self.forward_steps,
            backward_steps=self.backward_steps,
            stop_when_no_improvement=self.stop_when_no_improvement)
        self.filterbank_csp.run()
        
        self.multi_class = MultiClassWeightedVoting(
                                    self.binary_csp.train_labels_full_fold, 
                                    self.binary_csp.test_labels_full_fold,
                                    self.filterbank_csp.train_pred_full_fold,
                                    self.filterbank_csp.test_pred_full_fold,
                                    self.class_pairs)
        self.multi_class.run()
        

class CSPTrainTest(CSPTrain):
    def __init__(self,filename, test_filename, **kwargs):
        self.test_filename = test_filename
        super(CSPTrainTest, self).__init__(filename, **kwargs)

    def run(self):
        log.info("Loading train set...")
        self.load_bbci_set()
        log.info("Loading test set...")
        self.load_bbci_test_set()
        log.info("Cleaning both sets...")
        self.clean_both_sets()
        log.info("Preprocessing train set...")
        self.preprocess_set()
        log.info("Preprocessing test set...")
        self.preprocess_test_set()
        self.remember_sensor_names()
        self.init_training_vars()
        log.info("Running Training...")
        self.run_training()

    def load_bbci_test_set(self):
        #TODELAY: do we even need to assign bbci_test_set to this object?
        self.bbci_test_set = BBCIDataset(self.test_filename, 
            sensor_names=self.load_sensor_names)
        self.bbci_test_set.load_continuous_signal()
        self.bbci_test_set.add_markers()
        self.test_cnt = self.bbci_test_set.cnt
        del self.bbci_test_set.cnt # save memory

    def clean_both_sets(self):
        cleaner = BBCITwoSetsCleaner(second_filename=self.test_filename,
            load_sensor_names=self.load_sensor_names,
            rejection_var_ival=self.rejection_var_ival, 
            rejection_blink_ival=self.rejection_blink_ival,
            max_min=self.max_min, 
            whisker_percent=self.whisker_percent, 
            whisker_length=self.whisker_length)
        
        (rejected_chans, rejected_trials, clean_trials) = cleaner.clean(
            self.cnt, self.filename)
        
        self.rejected_chans = rejected_chans
        self.rejected_trials = rejected_trials
        self.clean_trials = clean_trials
        # do not clean test trials for now...
        self.rejected_test_trials = []
        self.clean_test_trials = range(len(self.test_cnt.markers))
        # do not remove rejected channels yet to allow calling
        # this function several times with same result
        
        assert np.intersect1d(self.clean_trials, self.rejected_trials).size == 0
        assert np.intersect1d(self.clean_test_trials, 
            self.rejected_test_trials).size == 0
        assert np.array_equal(np.union1d(self.clean_test_trials, 
            self.rejected_test_trials), range(len(self.test_cnt.markers)))
        assert np.array_equal(np.union1d(self.clean_trials, 
            self.rejected_trials), range(len(self.cnt.markers)))

    def preprocess_test_set(self):
        # Remove same chans as in train set...        
        self.test_cnt = select_channels(self.test_cnt, self.rejected_chans, 
            invert=True)
        if self.sensor_names is not None:
            self.sensor_names = sort_topologically(self.sensor_names)
            self.test_cnt = select_channels(self.test_cnt, self.sensor_names)
        self.test_cnt = resample_cnt(self.test_cnt, newfs=self.resample_fs)
        if self.low_cut_off_hz is not None:
            self.test_cnt = highpass_cnt(self.test_cnt, low_cut_off_hz=self.low_cut_off_hz)
        if self.common_average_reference is True:
            self.test_cnt = common_average_reference_cnt(self.test_cnt)


    def init_training_vars(self):
        assert self.restricted_n_trials is None, "Not implemented yet"
        self.filterbands = generate_filterbank(min_freq=self.min_freq,
            max_freq=self.max_freq, last_low_freq=self.last_low_freq, 
            low_width=self.low_width, high_width=self.high_width)
        self.class_pairs = list(itertools.combinations([0,1,2,3],2))
        train_fold = self.clean_trials
        num_all_train_trials = len(self.rejected_trials) + len(self.clean_trials)
        assert num_all_train_trials == len(self.cnt.markers), ("all markers should "
            "correspond to one train trial")
        # you have to add number of train trials to the test indices, since
        # train and test will be concatenated...
        test_fold = np.array(self.clean_test_trials) + num_all_train_trials
        self.folds = [{'train': train_fold, 'test': test_fold}]
        assert np.intersect1d(self.folds[0]['test'], 
            self.folds[0]['train']).size == 0

        # merge cnts!!
        self.cnt = append_cnt(self.cnt, self.test_cnt)

def generate_filterbank(min_freq, max_freq, last_low_freq,
        low_width, high_width):
    assert isinstance(min_freq, int) or min_freq.is_integer()
    assert isinstance(max_freq, int) or max_freq.is_integer()
    assert isinstance(last_low_freq, int) or last_low_freq.is_integer()
    assert isinstance(low_width, int) or low_width.is_integer()
    assert isinstance(high_width, int) or high_width.is_integer()
    
    assert high_width % 2  == 0
    assert low_width % 2  == 0
    assert (last_low_freq - min_freq) % low_width  == 0, ("last low freq "
        "needs to be exactly the center of a low_width filter band")
    assert max_freq >= last_low_freq
    assert (max_freq == last_low_freq or  
            (max_freq - (last_low_freq + low_width/2 + high_width/2)) % 
        high_width == 0), ("max freq needs to be exactly the center "
            "of a filter band")
    low_centers = range(min_freq,last_low_freq+1, low_width)
    high_start = last_low_freq + low_width/2 + high_width/2
    high_centers = range(high_start, max_freq+1, high_width)
    
    low_band = np.array([np.array(low_centers) - low_width/2, 
                         np.array(low_centers) + low_width/2]).T
    low_band = np.maximum(0.5, low_band)
    high_band = np.array([np.array(high_centers) - high_width/2, 
                         np.array(high_centers) + high_width/2]).T
    filterbank = np.concatenate((low_band, high_band))
    return filterbank

class CSPRetrain():
    """ CSP Retraining on existing filters computed previously."""
    def __init__(self, trainer_filename, num_selected_features="asbefore",
            num_selected_filterbands="asbefore",forward_steps=2,
            backward_steps=1, stop_when_no_improvement=False):
        self.trainer_filename = trainer_filename
        self.num_selected_features = num_selected_features
        self.num_selected_filterbands = num_selected_filterbands
        self.forward_steps = forward_steps
        self.backward_steps = backward_steps
        self.stop_when_no_improvement = stop_when_no_improvement

    def get_trainer(self):
        """ just for later saving"""
        return self.trainer
        
    def run(self):
        log.info("Loading trainer...")
        self.trainer = serial.load(self.trainer_filename)
        if self.num_selected_features == "asbefore":
            self.num_selected_features = self.trainer.filterbank_csp.num_features
        if self.num_selected_filterbands == "asbefore":
            self.num_selected_filterbands = self.trainer.filterbank_csp.num_filterbands
        # For later storage, remember selected features and filterbands
        # TODELAY: solve this more cleanly during saving or sth :)
        self.trainer.original_params['num_selected_features'] = \
            self.num_selected_features
        self.trainer.original_params['num_selected_filterbands'] = \
            self.num_selected_filterbands
        recreate_filterbank(self.trainer, self.num_selected_features,
            self.num_selected_filterbands, self.forward_steps,
            self.backward_steps, self.stop_when_no_improvement)
        
        log.info("Rerunning filterbank...")
        self.trainer.filterbank_csp.run()
        recreate_multi_class(self.trainer)
        log.info("Rerunning multiclass...")
        self.trainer.multi_class.run()

def recreate_filterbank(train_csp_obj, num_features, num_filterbands,
        forward_steps, backward_steps, stop_when_no_improvement):
    train_csp_obj.filterbank_csp = FilterbankCSP(train_csp_obj.binary_csp,
        num_features, num_filterbands, 
            forward_steps=forward_steps,
            backward_steps=backward_steps,
            stop_when_no_improvement=stop_when_no_improvement)

def recreate_multi_class(train_csp_obj):
    """ Assumes filterbank + possibly binary csp was rerun and
    recreates multi class weighted voting object 
    with new labels + predictions. """
    train_csp_obj.multi_class = MultiClassWeightedVoting(
        train_csp_obj.binary_csp.train_labels_full_fold, 
        train_csp_obj.binary_csp.test_labels_full_fold,
        train_csp_obj.filterbank_csp.train_pred_full_fold,
        train_csp_obj.filterbank_csp.test_pred_full_fold,
        train_csp_obj.class_pairs)