import copy
from datetime import datetime
import logging
import matplotlib.pyplot as plt
import multiprocessing
import numpy as np
import os
import pandas as pd
import seaborn as sns
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import ShuffleSplit
import sys
from tqdm import tqdm

from gendis.operators import (
    add_shapelet, 
    remove_shapelet,
    replace_shapelet,
    smooth_shapelet
)
from gendis.operators import (
    crossover_AND,
    crossover_uniform
)

from gendis.genetic import GeneticExtractor
from gendis.subgroup_quality import SubgroupQuality

from util import parse_args, save_json, setup_logging
from viz import plot_error_distributions, plot_best_matching_shaps

# Setup
args = parse_args()
input_file_path = args.input_file_path
input_filename = os.path.splitext(os.path.basename(input_file_path))[0]
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
results_folder = f"./{input_filename}_{timestamp}"
setup_logging(results_folder, timestamp)


logging.info('[INFO] Script started')
logging.debug(f'[INFO] Received input file path: {input_file_path}')

# Reading, splitting data

df = pd.read_csv(input_file_path)

logging.info('[INFO] Error column info:')
logging.info(df.error.describe().to_dict())

logging.info('[INFO] Labels info:')
logging.info(df.label.value_counts().to_dict())

plot_error_distributions(df, results_folder)

X = df.drop(columns=['error', 'label'])
y = df['error']

sss = ShuffleSplit(n_splits=1, test_size=0.1, random_state=0)
sss.get_n_splits(X, y)
train_index, test_index = next(sss.split(X, y))

X_train, y_train = X.iloc[train_index], y.iloc[train_index]
X_test, y_test = X.iloc[test_index], y.iloc[test_index]

# Gendis initialization
COVERAGE_ALPHA = 0.8
SUBGROUP_SIZE_BETA = 0.5
DIST_THRESHOLD = 10
subgroup_args = {
    "coverage_alpha": COVERAGE_ALPHA,
    "subgroup_size_beta": SUBGROUP_SIZE_BETA,
    "dist_threshold": DIST_THRESHOLD
}

logging.info('[INFO] Parameters SubgroupDistance')
logging.info({
    'coverage_alpha': COVERAGE_ALPHA,
    'subgroup_size_beta': SUBGROUP_SIZE_BETA,
    'shapelet_distance_threshold': DIST_THRESHOLD
})


mut_ops= [add_shapelet, remove_shapelet, replace_shapelet]
cx_ops = [crossover_uniform]

subgroup_quality_func = SubgroupQuality(
    distance_function=SubgroupQuality.simple_mean, 
    shapelet_dist_threshold=DIST_THRESHOLD,
    sg_size_beta=SUBGROUP_SIZE_BETA,
    standardize=False
)

args = {
    "k": 10,
    "coverage_alpha": COVERAGE_ALPHA,
    "population_size": 200, 
    "iterations": 100,  
    "mutation_prob": 0.3, 
    "crossover_prob": 0.3,
    "max_shaps": 3,
    "wait": 20, 
    "min_len": 20,
    "max_len": 60,
    "n_jobs": multiprocessing.cpu_count() - 3,
    "verbose": False,
    "dist_threshold": DIST_THRESHOLD,
}
funcs = {
    "fitness": subgroup_quality_func,
    "mut_ops": mut_ops,
    "cx_ops": cx_ops
}

logging.info('[INFO] Parameters Gendis')
logging.info(args)

all_args = {**args, **subgroup_args}
save_json(all_args, f"{results_folder}/parameters.json")

# Preprocess and model fit
args = {**args, **funcs}
X_input, y_input = GeneticExtractor.preprocess_input(X_train, y_train)
gendis = GeneticExtractor(**args)
gendis.fit(X_input, y_input)

# Log results

logging.info('[INFO] Results')
logging.info('[INFO] Best individual stats')
logging.info(gendis.best["info"])
save_json(gendis.best["info"], f"{results_folder}/best_info.json")

logging.info('[INFO] Top-K info')
topk_info = [x['info'] for x in gendis.top_k]
logging.info(topk_info)
save_json(topk_info, f"{results_folder}/topk_info.json")

logging.info('[INFO] Top-K coverage')
logging.info(gendis.top_k_coverage)
save_json(
    {"coverage": gendis.top_k_coverage}, 
    f"{results_folder}/topk_coverage.json"
)

# Plot best matching shapelets

for i, ind in enumerate(gendis.top_k):
    plot_best_matching_shaps(
        gendis, ind, X_input, y_input,
        path=results_folder, plot_i=i
    )

gendis.save(f"{results_folder}/gendis.pickle")



