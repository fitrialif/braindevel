import numpy as np
from braindecode.datahandling.batch_iteration import compute_trial_start_end_samples
from braindecode.mywyrm.processing import create_cnt_y,\
    create_new_class_to_old_class

def create_cnt_y_start_end_marker(cnt, start_marker_def, end_marker_def,
    segment_ival, timeaxis=-2, trial_classes=None):
    """Segment ival is : (offset to start marker, offset to end marker)"""
    start_to_end_value = dict()
    for class_name in start_marker_def:
        start_marker_vals = start_marker_def[class_name]
        end_marker_vals = end_marker_def[class_name]
        assert len(start_marker_vals) == 1
        assert len(end_marker_vals) == 1
        start_to_end_value[start_marker_vals[0]] = end_marker_vals[0] 

    # Assuming start marker vals are 1 ... n_classes
    # Otherwise change code...
    all_start_marker_vals = start_to_end_value.keys()
    n_classes = np.max(all_start_marker_vals)
    
    # You might disable this if you have checked trial_classes implementation here
    assert (trial_classes is not None) or (
        np.array_equal(np.sort(all_start_marker_vals), range(1, n_classes+1))), (
        "Assume start marker values are from 1...n_classes if trial classes not given")
    all_end_marker_vals = start_to_end_value.values()
    
    if trial_classes is not None:
        old_class_to_new_class = create_new_class_to_old_class(start_marker_def,
            trial_classes)
    y = np.zeros((cnt.data.shape[0], np.max(all_start_marker_vals)), dtype= np.int32)
    i_marker = 0
    while i_marker < len(cnt.markers):
        # first find start marker
        while ((i_marker < len(cnt.markers)) and 
            (cnt.markers[i_marker][1] not in all_start_marker_vals)):
            i_marker += 1
        if (i_marker < len(cnt.markers)):
            start_marker_ms = cnt.markers[i_marker][0]
            start_marker_val = cnt.markers[i_marker][1]
            # find end marker
            while ((i_marker < len(cnt.markers)) and 
                (cnt.markers[i_marker][1] not in all_end_marker_vals)):
                i_marker += 1
            assert i_marker < len(cnt.markers), "There should be an end marker for each start marker(?)"
            end_marker_ms = cnt.markers[i_marker][0]
            end_marker_val = cnt.markers[i_marker][1]
            assert end_marker_val == start_to_end_value[start_marker_val]
            
            first_index = np.searchsorted(cnt.axes[timeaxis], start_marker_ms + segment_ival[0])
            last_index = np.searchsorted(cnt.axes[timeaxis], end_marker_ms+segment_ival[1])
            if trial_classes is not None:
                # -1 because before is 1-based matlab-indexing(!)
                i_class = int(old_class_to_new_class[int(start_marker_val)] - 1)
            else:
                # -1 because before is 1-based matlab-indexing(!)
                i_class = int(start_marker_val - 1)
            y[first_index:last_index, i_class] = 1 
    return y

class PipelineSegmenter(object):
    def __init__(self, segmenters):
        self.segmenters = segmenters
    def segment(self, cnt):
        y, classnames = self.segmenters[0].segment(cnt)
        for segmenter in self.segmenters[1:]:
            y, classnames = segmenter.segment(cnt,y, classnames)
        return y, classnames

class MarkerSegmenter(object):
    def __init__(self, segment_ival, marker_def, trial_classes, end_marker_def=None):
        self.segment_ival = segment_ival
        self.marker_def = marker_def
        self.end_marker_def = end_marker_def
        self.trial_classes = trial_classes
        
    def segment(self, cnt):
        # marker segmenter, dann restrict range, dann restrict classes, dann evtl. add breaks
        assert np.all([len(labels) == 1 for labels in 
                self.marker_def.values()]), (
                "Expect only one label per class, otherwise rewrite...")
        # get class names, assume they are sorted by marker codes
        class_names = sorted(self.marker_def.keys(), 
            key= lambda k: self.marker_def[k][0])
        if self.end_marker_def is None:
            y = create_cnt_y(cnt, self.segment_ival,self.marker_def,
                trial_classes=self.trial_classes)
        else:
            y = create_cnt_y_start_end_marker(cnt,self.marker_def, self.end_marker_def,
                segment_ival=self.segment_ival,
                trial_classes=self.trial_classes)
        return y, class_names

class RestrictTrialRange(object):
    def __init__(self, start_trial, stop_trial):
        self.start_trial = start_trial
        self.stop_trial = stop_trial
        
    def segment(self, cnt, y, class_names):
        # dont modify original y
        y = np.copy(y)
        if (self.start_trial is not None) or (self.stop_trial is not None):
            trial_starts, trial_ends = compute_trial_start_end_samples(y,
                check_trial_lengths_equal=False)
            if self.start_trial is not None:
                y[:trial_starts[self.start_trial] - 1] = 0
            if self.stop_trial is not None:
                y[trial_starts[self.stop_trial] - 1:] = 0
        return y, class_names
    
class RestrictTrialClasses(object):
    def __init__(self, class_names):
        self.class_names = class_names
    
    def segment(self, cnt, y, class_names):
        # dont modify original y
        y = np.copy(y)
        indexes_to_keep = [class_names.index(name) for name in self.class_names]
        y = y[:,np.array(indexes_to_keep)]
        return y, self.class_names
    
class AddTrialBreaks(object):
    def __init__(self, min_length_ms, max_length_ms, start_offset_ms, stop_offset_ms):
        self.min_length_ms = min_length_ms
        self.max_length_ms = max_length_ms
        self.start_offset_ms = start_offset_ms
        self.stop_offset_ms = stop_offset_ms
        
    def segment(self, cnt, y, class_names):
        # add new class vector, for now empty
        y = np.concatenate((y, y[:,0:1] * 0), axis=1)
        
        trial_starts, trial_ends = compute_trial_start_end_samples(y, check_trial_lengths_equal=False)
        start_offset = int(np.round(self.start_offset_ms * float(cnt.fs) / 1000.0))
        stop_offset = int(np.round(self.stop_offset_ms * float(cnt.fs) / 1000.0))
        for break_start, break_stop in zip(trial_ends[:-1] + 1, trial_starts[1:]):
            break_len = break_stop - break_start
            break_len_ms = break_len * 1000.0 / float(cnt.fs)
            if (break_len_ms >= self.min_length_ms) and (break_len_ms <= self.max_length_ms):
                start = break_start + start_offset
                stop  = break_stop + stop_offset
                y[start:stop,-1] = 1
        new_class_names = class_names + ["TrialBreak"]
        return y, new_class_names