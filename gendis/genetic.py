# Standard lib
import copy
import array
import time

# "Standard" data science libs
import numpy as np
from math import ceil, floor, isinf
import matplotlib.pyplot as plt
import pandas as pd

# Serialization
import pickle

# Evolutionary algorithms framework
from deap import base, creator, tools

# Parallelization
from pathos.multiprocessing import ProcessingPool as Pool
import multiprocessing

# ML
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.preprocessing import StandardScaler
from sklearn.utils.validation import check_is_fitted

try:
    from individual import Shapelet, ShapeletIndividual
except:
    from gendis.individual import Shapelet, ShapeletIndividual

try:
    from shapelets_distances import (
        calculate_shapelet_dist_matrix, dtw, _pdist_location, euclidean
    )
except:
    from gendis.shapelets_distances import (
        calculate_shapelet_dist_matrix, dtw, _pdist_location, euclidean
    )

import logging

# todo remove shapelet
try:
    from operators import (
        random_shapelet, kmeans,
        crossover_AND, crossover_uniform,
        add_shapelet, remove_shapelet, replace_shapelet, smooth_shapelet
    )
    from LRUCache import LRUCache

    

except:
    from gendis.operators import (
        random_shapelet, kmeans,
        crossover_AND, crossover_uniform,
        add_shapelet, remove_shapelet, replace_shapelet, smooth_shapelet
    )
    from gendis.LRUCache import LRUCache

from dtaidistance.preprocessing import differencing

# Ignore warnings
import warnings
warnings.filterwarnings('ignore')


class GeneticExtractor(BaseEstimator, TransformerMixin):
    """Feature selection with genetic algorithm.

    Parameters
    ----------
    population_size : int
        The number of individuals in our population. Increasing this parameter
        increases both the runtime per generation, as the probability of
        finding a good solution.

    iterations : int
        The maximum number of generations the algorithm may run.

    wait : int
        If no improvement has been found for `wait` iterations, then stop

    add_noise_prob : float
        The chance that gaussian noise is added to a random shapelet from a
        random individual every generation

    add_shapelet_prob : float
        The chance that a shapelet is added to a random shapelet set every gen

    remove_shapelet_prob : float
        The chance that a shapelet is deleted to a random shap set every gen

    crossover_prob : float
        The chance that of crossing over two shapelet sets every generation

    normed : boolean
        Whether we first have to normalize before calculating distances

    n_jobs : int
        The number of threads to use

    verbose : boolean
        Whether to print some statistics in every generation

    plot : object
        Whether to plot the individuals every generation (if the population 
        size is <= 20), or to plot the fittest individual

    Attributes
    ----------
    shapelets : array-like
        The fittest shapelet set after evolution
    label_mapping: dict
        A dictionary that maps the labels to the range [0, ..., C-1]

    Example
    -------
    An example showing genetic shapelet extraction on a simple dataset:

    >>> from tslearn.generators import random_walk_blobs
    >>> from genetic import GeneticExtractor
    >>> from sklearn.linear_model import LogisticRegression
    >>> import numpy as np
    >>> np.random.seed(1337)
    >>> X, y = random_walk_blobs(n_ts_per_blob=20, sz=64, noise_level=0.1)
    >>> X = np.reshape(X, (X.shape[0], X.shape[1]))
    >>> extractor = GeneticExtractor(iterations=5, population_size=10)
    >>> distances = extractor.fit_transform(X, y)
    >>> lr = LogisticRegression()
    >>> _ = lr.fit(distances, y)
    >>> lr.score(distances, y)
    1.0
    """
    def __init__(
        self,
        k,
        fitness,
        population_size, 
        iterations, 
        mutation_prob, 
        crossover_prob,
        coverage_alpha=0.5,
        wait=10, 
        plot=None, 
        max_shaps=None, 
        n_jobs=1, 
        max_len=None,
        min_len=0, 
        init_ops=[random_shapelet],
        cx_ops=[crossover_AND, crossover_uniform], 
        mut_ops=[add_shapelet, remove_shapelet, replace_shapelet, smooth_shapelet],
        dist_threshold=1.0,
        verbose=False, 
        normed=False, 
    ):
        self._set_fitness_function(fitness)
        self.k = k
        # Hyper-parameters
        self.population_size = population_size
        self.iterations = iterations
        self.mutation_prob = mutation_prob
        self.crossover_prob = crossover_prob
        self.coverage_alpha = coverage_alpha
        self.plot = plot
        self.wait = wait
        self.verbose = verbose
        self.n_jobs = n_jobs
        self.normed = normed
        self.min_len = min_len
        self.max_len = max_len
        self.max_shaps = max_shaps
        self.init_ops = init_ops
        self.cx_ops = cx_ops
        self.mut_ops = mut_ops
        self.is_fitted = False
        self.dist_threshold = dist_threshold

        # Attributes
        self.label_mapping = {}
        self.shapelets = []
        self.top_k = None

        self.apply_differencing = True
        # self.dist_function = dtw, euclidean, _pdist_location
        self.dist_function = euclidean

    def _set_fitness_function(self, fitness):
        assert fitness is not None, "Please include a fitness function via fitness parameter.\
            See fitness.logloss_fitness for classification or \
            subgroup_distance.SubgroupDistance for subgroup search"
        assert callable(fitness)
        self.fitness = fitness

    @staticmethod
    def preprocess_input(X, y):
        _X = copy.deepcopy(X)
        if isinstance(_X, pd.DataFrame):
            _X = _X.values
        _X = np.apply_along_axis(lambda s: differencing(s, smooth=None), 1, _X)

        y = copy.deepcopy(y)
        if isinstance(y, pd.Series):
            y = y.values

        return _X, y

    def _print_statistics(self, stats, start):
        if self.it == 1:
            # Print the header of the statistics
            print('it\t\tavg\t\tstd\t\tmax\t\ttime')
            #print('it\t\tavg\t\tmax\t\ttime')

        print('{}\t\t{}\t\t{}\t\t{}\t{}'.format(
        # print('{}\t\t{}\t\t{}\t{}'.format(
            self.it,
            np.around(stats['avg'], 4),
            np.around(stats['std'], 3),
            np.around(stats['max'], 6),
            np.around(time.time() - start, 4),
        ))

    def _update_best_individual(self, it, new_ind):
        """Update the best individual if we found a better one"""
        ind_score = self._eval_individual(new_ind)
        self.best = {
            'it': it,
            'score': ind_score['value'][0],
            'info': ind_score['info'],
            'shapelets': new_ind
        }
 
    def _create_individual(self, n_shapelets=None):
        """Generate a random shapelet set"""
        n_shapelets = 1
        init_op = np.random.choice(self.init_ops)
        return init_op(
            X=self.X, 
            n_shapelets=n_shapelets, 
            min_len_series=self._min_length_series, 
            max_len=self.max_len, 
            min_len=self.min_len
        )

    def _eval_individual(self, shaps):
            """Evaluate the fitness of an individual"""
            D, _ = calculate_shapelet_dist_matrix(
                self.X, shaps, 
                dist_function=self.dist_function, 
                return_positions=False,
                cache=self.cache
                )

            return self.fitness(D=D, y=self.y, shaps=shaps)

            # if return_info: return fit
            # return fit["value"]

    def _mutate_individual(self, ind, toolbox):
        """Mutate an individual"""
        if np.random.random() < self.mutation_prob:
            mut_op = np.random.choice(self.mut_ops)
            mut_op(ind, toolbox)
            ind.reset()

    def _cross_individuals(self, ind1, ind2):
        """Cross two individuals"""
        if np.random.random() < self.crossover_prob:
            cx_op = np.random.choice(self.deap_cx_ops)
            cx_op(ind1, ind2)
            ind1.reset()
            ind2.reset()

    def _safe_std(*args, **kwargs):
        try:
            return np.std(*args, **kwargs)
        except ZeroDivisionError:
            return 0

    @staticmethod
    def rebuild_diffed(series):
        return np.insert(np.cumsum(series), 0, 0)

    def _early_stopping_check(self):
        return (
            self.it - self.last_top_k_change > self.wait
        )

    def _update_coverage(self, subgroup, coverage):
        # Since we have just created a new subgroup,
        # we add +1 to every subgroup member instance counts
        coverage[subgroup] += 1
        # Raise alpha to the 'counts' for each instance
        # That's how much each instance will contribute to a next iteration
        base = [self.coverage_alpha]
        return np.power(base, coverage), coverage

    def _coverage_factor(self, weights, subgroup):
        """Multiplicative weighted covering score"""
        in_sg_weights = weights[subgroup].sum()
        sg_weights_total = subgroup.sum() * self.coverage_alpha
        return in_sg_weights / sg_weights_total

    def _format_ind_topk(self):
        top_k_formatted = []
        for ind in self.top_k:
            info = ind.info
            info["coverage_weight"] = ind.coverage_weight
            data = {
                "shaps": ind,
                "info": info,
                "subgroup": ind.subgroup,
                "coverage_weight": ind.coverage_weight,
            }
            top_k_formatted.append(data)

        self.top_k  = top_k_formatted
            
    def _update_top_k(self, pop, it, tools):
        print(f"[INFO] Updating TOP-K it{it}")
        coverage = np.ones(len(self.X))
        weights = np.power([self.coverage_alpha], coverage)

        pop = list(map(ShapeletIndividual.clone, pop))

        if self.top_k is None:
            pop_star = pop
            self.top_k_ids = []
            self.top_k_coverage = np.array([])
        else:
            pop_star = self.top_k + pop

        best = max(pop_star, key=lambda ind: ind.fitness.values[0])
        best.coverage_weight = 1.0
        new_top_k = [best]
        new_top_k_ids = set([best.uuid])

        k = 0
        while k < self.k:
            # Update coverage and coverage_weights
            weights, coverage = self._update_coverage(
                best.subgroup, coverage
            )
            
            # Calculate weighted scores
            # For each individual, update fitness based on weights
            fitness_values = []
            coverage_factors = []
            for ind in pop_star:
                ind.coverage_weight = self._coverage_factor(weights, ind.subgroup)
                fitness_values.append(ind.fitness.values[0])
                coverage_factors.append(ind.coverage_weight)

            fitness_values = np.array(fitness_values)
            coverage_factors = np.array(coverage_factors)
            weighted_scores = fitness_values * coverage_factors

            # Get the individual with maximum weighted score
            found_new_best = False

            while not found_new_best and pop_star:
                # This avoids duplicates individuals
                max_index = np.argmax(weighted_scores)
                best_weighted = weighted_scores[max_index]
                best_cov = coverage_factors[max_index]

                weighted_scores = np.delete(weighted_scores, max_index)
                coverage_factors = np.delete(coverage_factors, max_index)
                best = pop_star.pop(max_index)
                found_new_best = (best.uuid not in new_top_k_ids)

            new_top_k.append(best)
            new_top_k_ids.add(best.uuid)
            k+=1
            
        logging.debug(f"Top-K ids: {new_top_k_ids}")

        # Early stopping strategy based on coverage
        if not np.array_equal(coverage, self.top_k_coverage):
            self.last_top_k_change = it
        
        self.top_k_coverage = coverage
        self.top_k = list(map(ShapeletIndividual.clone, new_top_k))

        self._print_pop(self.top_k, tools)
        self.top_k_ids = new_top_k_ids

    def assert_healthy_individual(self, ind, msg):
        for shap in ind:
            try:
                assert isinstance(shap, Shapelet), f"Expected a Shapelet instance [{msg}]."
                assert hasattr(shap, 'id'), f"Shapelet does not have an 'id' attribute."
            except Exception as e:
                print(ind)
                raise(e)

    def _create_individual_manual(self, creator, X, row, start, end):
        shapelet = Shapelet(X[row, start:end])
        individual = ShapeletIndividual([shapelet])
        ind = creator.Individual(individual)
        print(ind.uuid)
        return ind

    def _print_pop(self, pop, tools):
        best_pop = tools.selBest(pop, len(pop))
        logging.info(f'[DEBUG] COMPILING RESULTS:{self.it}') 
        for i in best_pop:
            logging.info(f'[INFO] fitness={i.fitness.values},\ti={i.uuid}') 

    def fit(self, X, y):
        """Extract shapelets from the provided timeseries and labels.

        Parameters
        ----------
        X : array-like, shape = [n_ts, ]
            The training input timeseries. Each timeseries must be an array,
            but the lengths can be variable

        y : array-like, shape = [n_samples]
            The target values.
        """ 
        self.X = X
        self.y = y

        self._min_length_series = min([len(x) for x in self.X])

        if self._min_length_series <= 4:
            raise Exception('Time series should be of at least length 4!')

        if self.max_len is None:
            if len(self.X[0]) > 20:
                self.max_len = len(self.X[0]) // 2
            else:
                self.max_len = len(self.X[0])

        if self.max_shaps is None:
            self.max_shaps = int(np.sqrt(self._min_length_series)) + 1

        self.cache = LRUCache(2048)

        creator.create("FitnessMax", base.Fitness, weights=[1.0])

        # Individual are lists (of shapelets (list))
        creator.create("Individual", ShapeletIndividual, fitness=creator.FitnessMax)

        # Keep a history of the evolution
        self.history = []

        # Register all operations in the toolbox
        toolbox = base.Toolbox()
        toolbox.register("clone", ShapeletIndividual.clone)

        if self.n_jobs == -1:
            self.n_jobs = multiprocessing.cpu_count()

        if self.n_jobs > 1:
            pool = Pool(self.n_jobs)
            toolbox.register("map", pool.map)
        else:
            toolbox.register("map", map)

        # Register all our operations to the DEAP toolbox
        # toolbox.register("merge", merge_crossover)
        self.deap_cx_ops = []
        for i, cx_op in enumerate(self.cx_ops):
            toolbox.register(f"cx{i}", cx_op)
            self.deap_cx_ops.append(getattr(toolbox, (f"cx{i}")))
        
        self.deap_mut_ops = []
        for i, mut_op in enumerate(self.mut_ops):
            toolbox.register(f"mutate{i}", mut_op)
            self.deap_mut_ops.append(getattr(toolbox, (f"mutate{i}")))

        toolbox.register("create", self._create_individual)
        toolbox.register(
            "individual", tools.initIterate, creator.Individual, toolbox.create)
        toolbox.register("population", tools.initRepeat, list, toolbox.individual)

        toolbox.register("evaluate", self._eval_individual)
        # Small tournaments to ensure diversity
        toolbox.register("select", tools.selTournament, tournsize=2)

        # Set up the statistics. We will measure the mean, std dev and max
        stats = tools.Statistics(key=lambda ind: ind.fitness.values[0])

        stats.register("avg", lambda arr: np.ma.masked_invalid(arr).mean())
        stats.register("std", lambda arr: np.ma.masked_invalid(arr).std())
        stats.register("max", lambda arr: np.ma.masked_invalid(arr).max())
        stats.register("min", lambda arr: np.ma.masked_invalid(arr).min())
        # stats.register("q25", lambda x: np.quantile(x, 0.25))
        # stats.register("q75", lambda x: np.quantile(x, 0.75))

        # Initialize the population and calculate their initial fitness values
        pop = toolbox.population(n=self.population_size)
        fitnesses = list(map(toolbox.evaluate, pop))
        for ind, fit in zip(pop, fitnesses):
            while not fit["valid"]:
                remove_shapelet(ind, toolbox, remove_last=True)
                fit = toolbox.evaluate(ind)

            ind.fitness.values = fit["value"]
            ind.subgroup = fit["subgroup"]
            ind.info = fit["info"]
        
        # Keep track of the best iteration, in order to do stop after `wait`
        # generations without improvement
        self.it = 1
        self.best = {
            'it': self.it,
            'score': float('-inf'),
            'info': None,
            'shapelets': []
        }
        self.last_top_k_change = 0

        # Set up a matplotlib figure and set the axes
        height = int(np.ceil(self.population_size/4))
        if self.plot is not None and self.plot != 'notebook':
            if self.population_size <= 20:
                f, ax = plt.subplots(4, height, sharex=True)
            else:
                plt.figure(figsize=(15, 5))
                plt.xlim([0, len(self.X[0])])

        # The genetic algorithm starts here
        while self.it <= self.iterations:
            logging.info(f'[INFO] it:{self.it}') 

            # Early stopping
            if self._early_stopping_check(): break

            gen_start = time.time()

            # Clone the population into offspring
            offspring = list(map(toolbox.clone, pop))
            
            # Iterate over all individuals and apply CX with certain prob
            for child1, child2 in zip(offspring[::2], offspring[1::2]):
                self._cross_individuals(child1, child2)

            # Apply mutation to each individual with a certain probability
            for indiv in offspring:
                self._mutate_individual(indiv, toolbox)
            
            # Update the fitness values
            invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
            fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
            for ind, fit in zip(invalid_ind, fitnesses):
                # Search for shapelet until individual is valid
                while not fit["valid"]:
                    remove_shapelet(ind, toolbox, remove_last=True)
                    ind.pop_uuid()
                    fit = toolbox.evaluate(ind)
                
                ind.fitness.values = fit["value"]
                ind.subgroup = fit["subgroup"]
                ind.info = fit["info"]

            # Replace population and update hall of fame, statistics & history
            new_pop = toolbox.select(offspring, self.population_size - 1)
            # fittest_inds = tools.selBest(pop + offspring, 1)
            fittest_inds = max(
                pop + offspring, 
                key=lambda ind: ind.fitness.values[0]
            )
            pop[:] = new_pop + [fittest_inds]            
            it_stats = stats.compile(pop)
            self.history.append([self.it, it_stats])

            # Print our statistics
            if self.verbose:
                self._print_statistics(stats=it_stats, start=gen_start)

            # Have we found a new best score?
            if it_stats['max'] > self.best['score']:
                best_ind = tools.selBest(pop + offspring, 1)[0]
                self._update_best_individual(
                    it=self.it,
                    new_ind=best_ind,
                )

            # Update bag of best individuals
            self._update_top_k(pop, self.it, tools)
            self.it += 1

            # self._print_pop(pop, tools)
        

        self.pop = pop
        if self.apply_differencing:
            self.best["shaps_undiffed"] = [self.rebuild_diffed(x) for x in self.best["shapelets"]]
        
        self._format_ind_topk()
        self.is_fitted = True
        del self.X, self.y


    def transform(self, X, y, shapelets=None, return_positions=False, standardize=False):
        """After fitting the Extractor, we can transform collections of 
        timeseries in matrices with distances to each of the shapelets in
        the evolved shapelet set.

        Parameters
        ----------
        X : array-like, shape = [n_ts, ]
            The training input timeseries. Each timeseries must be an array,
            but the lengths can be variable

        Returns
        -------
        """
        assert self.is_fitted, "Fit the gendis model first calling fit()"

        if shapelets is None:
            shapelets = self.best['shapelets']
        
        assert len(shapelets) > 0, "No shapelets found"

        index = None
        if hasattr(X, 'index'):
            index = X.index

        D, L = calculate_shapelet_dist_matrix(
            X, shapelets, 
            dist_function=self.dist_function, 
            return_positions=return_positions,
            cache=None
        )

        subgroup, thresholds = self.fitness.get_set_subgroup(shapelets, D, y)

        if standardize:
            scaler = StandardScaler()
            return np.absolute(scaler.fit_transform(D))   

        cols = [f'D_{i}' for i in range(D.shape[1])]
        if return_positions:
            data = np.hstack((D, L))
            cols += [f'L_{i}' for i in range(L.shape[1])]

        data = np.hstack((data, subgroup.reshape(-1, 1)))
        cols.append('in_subgroup')
            
        return pd.DataFrame(data=data, columns=cols, index=index)

    def fit_transform(self, X, y):
        """Combine both the fit and transform method in one.

        Parameters
        ----------
        X : array-like, shape = [n_ts, ]
            The training input timeseries. Each timeseries must be an array,
            but the lengths can be variable

        y : array-like, shape = [n_samples]
            The target values.

        Returns
        -------
        D : array-like, shape = [n_ts, n_shaps]
            The matrix with distances
        """
        # First call fit, then transform
        self.fit(X, y)
        return self.transform(X)

    def save(self, path):
        """Write away all hyper-parameters and discovered shapelets to disk"""
        pickle.dump(self, open(path, 'wb+'))

    def get_subgroups(self, X_diffed, y, shapelets=None):
        """
        Get the subgroups based on the provided shapelets (if not provided, the best found by gendis).

        Parameters:
        - X (array-like): Input time series data.
        - y (array-like): Target labels for the time series data.
        - shapelets (array-like, optional): Shapelets used for transformation. If not provided,
        the function assumes that shapelets have already been calculated.

        Returns:
        - sg_indexes (array): Indexes of instances belonging to subgroups.
        - not_sg_indexes (array): Indexes of instances not belonging to subgroups.
        """
        assert self.is_fitted, "Fit the gendis model first calling fit()"

        shapelets = self.best["shapelets"]
        
        D, _ = calculate_shapelet_dist_matrix(
            X_diffed, shapelets, 
            dist_function=self.dist_function,
            return_positions=True,
            cache=None
        )

        subgroup = self.fitness.filter_subgroup_shapelets(D, y)
        [sg_indexes] = np.where(subgroup)
        [not_sg_indexes] = np.where(~subgroup)

        return sg_indexes, not_sg_indexes

    @staticmethod
    def load(path):
        """Instantiate a saved GeneticExtractor"""
        return pickle.load(open(path, 'rb'))

    def plot_series_and_shapelets(
        self,
        X,
        y,
        shapelets,
        indexes_to_plot,
        row_n = 5,
        col_m = 2,
        adjust_w = 1,
        adjust_h = 0.5,
        series_offset = 0,
    ):
        default_w, default_h = (4.8, 6.4)
        figsize = (col_m*default_w*adjust_w, row_n*default_h*adjust_h)

        fig, axs = plt.subplots(row_n, col_m, figsize=figsize)
        fig.tight_layout(pad=3.0)

        D, L = self.transform(X=X, shapelets=shapelets)

        for i, series_idx in enumerate(indexes_to_plot[series_offset:row_n*col_m]):
            row, col = i//col_m, i%col_m
            ax = axs[row][col]

            series = X.iloc[series_idx].values
            model_error = y.iloc[series_idx]
            ax.plot(series, alpha=0.3)
            ax.title.set_text(f'Series index {series_idx}, model error of {model_error:.2f}')
            
            for shap_idx, shap in enumerate(shapelets): 
                dist = D[series_idx][shap_idx]
                loc = L[series_idx][shap_idx]

                k = loc * float(len(series) - len(shap)) 
                start = floor(k)
                end = ceil(start + len(shap))
                shap_idx = list(range(start, end))

                # Dotted line if dist is above threshold
                fmt = '--' if dist > self.dist_threshold else '-'
                ax.plot(shap_idx, shap, fmt)


        i+=1
        # Remove unused axes
        while i < (row_n*col_m):
            row, col = i//col_m, i%col_m
            axs[row][col].remove()
            i+=1
        
        return plt
