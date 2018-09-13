from __future__ import division
import argparse
from sklearn.cluster import dbscan
from sklearn.metrics.pairwise import euclidean_distances
from scipy.sparse import csr_matrix
# from scipy import stats
from scipy.stats import normaltest, f_oneway, mannwhitneyu, zscore
from hicmatrix import HiCMatrix as hm
from hicmatrix.lib import MatrixFileHandler
from hicexplorer._version import __version__
from hicexplorer.utilities import toString
from hicexplorer.utilities import check_cooler
from hicexplorer.hicPlotMatrix import translate_region
import numpy as np
import logging
log = logging.getLogger(__name__)
from copy import deepcopy
import cooler
from multiprocessing import Process, Queue
import time

def parse_arguments(args=None):

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        description="""
Computes long range contacts within the given contact matrix
""")

    parserRequired = parser.add_argument_group('Required arguments')

    parserRequired.add_argument('--matrix', '-m',
                                help='The matrix to compute the long range contacts on.',
                                required=True)
    parserRequired.add_argument('--outFileName', '-o',
                                help='Outfile name with long range contacts clusters, should be a bedgraph.',
                                required=True)
    parserOpt = parser.add_argument_group('Optional arguments')
    parserOpt.add_argument('--zScoreThreshold', '-zt',
                           type=float,
                           default=8.0,
                           help='z-score threshold to detect long range interactions')
    parserOpt.add_argument('--zScoreMatrixName', '-zs',
                           help='Saves the computed z-score matrix')
    parserOpt.add_argument('--windowSize', '-w',
                           type=int,
                           default=4,
                           help='Window size')

    parserOpt.add_argument('--pValue', '-p',
                           type=float,
                           default=0.05,
                           help='p-value')
    parserOpt.add_argument('--qValue', '-q',
                           type=float,
                           default=0.05,
                           help='q value for FDR')

    parserOpt.add_argument('--peakInteractionsThreshold', '-pit',
                           type=int,
                           default=10,
                           help='The minimum number of interactions a detected peaks needs to have to be considered.')
    parserOpt.add_argument('--maxLoopDistance', '-mld',
                           type=int,
                           help='maximum distance of a loop, usually loops are within a distance of ~2MB.')

    parserOpt.add_argument('--chromosomes',
                           help='Chromosomes and order in which the chromosomes should be saved. If not all chromosomes '
                           'are given, the missing chromosomes are left out. For example, --chromosomeOrder chrX will '
                           'export a matrix only containing chromosome X.',
                           nargs='+')

    parserOpt.add_argument('--region',
                           help='The format is chr:start-end.',
                           required=False)
    parserOpt.add_argument('--threads',
                           help='The chromosomes are processed independent of each other. If the number of to be '
                           'processed chromosomes is greater than one, each chromosome will be computed with on its own core.',
                           required=False,
                           default=4,
                           type=int
                           )
    parserOpt.add_argument('--help', '-h', action='help',
                           help='show this help message and exit')

    parserOpt.add_argument('--version', action='version',
                           version='%(prog)s {}'.format(__version__))

    return parser


def _sum_per_distance(pSum_per_distance, pData, pDistances, pN):
    distance_count = np.zeros(len(pSum_per_distance))
    for i in range(pN):
        pSum_per_distance[pDistances[i]] += pData[i]
        distance_count[pDistances[i]] += 1
    return pSum_per_distance, distance_count


def compute_zscore_matrix(pMatrix):

    instances, features = pMatrix.nonzero()

    pMatrix.data = pMatrix.data.astype(float)
    data = deepcopy(pMatrix.data)
    distances = np.absolute(instances - features)
    sigma_2 = np.zeros(pMatrix.shape[0])

    # shifts all values by one, but 0 / mean is prevented
    sum_per_distance = np.ones(pMatrix.shape[0])
    np.nan_to_num(data, copy=False)

    sum_per_distance, distance_count = _sum_per_distance(
        sum_per_distance, data, distances, len(instances))
    # compute mean

    mean_adjusted = sum_per_distance / distance_count
    # compute sigma squared
    for i in range(len(instances)):
        sigma_2[distances[i]
                ] += np.square(data[i] - mean_adjusted[distances[i]])

    sigma_2 /= distance_count
    sigma = np.sqrt(sigma_2)

    for i in range(len(instances)):
        data[i] = (pMatrix.data[i] - mean_adjusted[distances[i]]) / \
            sigma[distances[i]]

    return csr_matrix((data, (instances, features)), shape=(pMatrix.shape[0], pMatrix.shape[1]))


def compute_long_range_contacts(pHiCMatrix, pZscoreMatrix, pZscoreThreshold, pWindowSize, pPValue, pQValue, pPeakInteractionsThreshold):

    # keep only z-score values if they are higher than pThreshold
    # keep: z-score value, (x, y) coordinate

    # keep only z-score values (peaks) if they have more interactions than pPeakInteractionsThreshold
    zscore_matrix = pZscoreMatrix
    zscore_matrix.eliminate_zeros()
    instances, features = zscore_matrix.nonzero()
    data = zscore_matrix.data.astype(float)

    log.debug('len(instances) {}, len(features) {}'.format(
        len(instances), len(features)))
    log.debug('len(data) {}'.format(len(data)))
    # filter by threshold
    mask = data >= pZscoreThreshold
    log.debug('len(data) {}, len(mask) {}'.format(len(data), len(mask)))

    mask_interactions = pHiCMatrix.matrix.data > pPeakInteractionsThreshold

    mask = np.logical_and(mask, mask_interactions)
    data = data[mask]

    instances = instances[mask]
    features = features[mask]

    if len(features) == 0:
        log.info('No loops detected.')
        return None, None
    candidates = [*zip(instances, features)]

    candidates, pValueList = candidate_uniform_distribution_test(
        pHiCMatrix, candidates, pWindowSize, pPValue, pQValue)
    return candidates, pValueList
    # return cluster_to_genome_position_mapping(pHiCMatrix, candidates, pValueList, pMaxLoopDistance)


def candidate_uniform_distribution_test(pHiCMatrix, pCandidates, pWindowSize, pPValue, pQValue):
    # this function test if the values in the neighborhood of a
    # function follow a normal distribution, given the significance level pValue.
    # if they do, the candidate is rejected, otherwise accepted
    # The neighborhood is defined as: the square around a candidate i.e. x-pWindowSize,  :
    accepted_index = []
    mask = []
    pvalues = []
    deleted_index = []
    high_impact_values = []
    x_max = pHiCMatrix.matrix.shape[0]
    y_max = pHiCMatrix.matrix.shape[1]
    maximal_value = 0

    mask = []

    for i, candidate in enumerate(pCandidates):

        start_x = candidate[0] - \
            pWindowSize if candidate[0] - pWindowSize > 0 else 0
        end_x = candidate[0] + pWindowSize if candidate[0] + \
            pWindowSize < x_max else x_max
        start_y = candidate[1] - \
            pWindowSize if candidate[1] - pWindowSize > 0 else 0
        end_y = candidate[1] + pWindowSize if candidate[1] + \
            pWindowSize < y_max else y_max

        neighborhood = pHiCMatrix.matrix[start_x:end_x,
                                         start_y:end_y].toarray().flatten()

        expected_neighborhood = np.random.uniform(low=np.min(
            neighborhood), high=np.max(neighborhood), size=len(neighborhood))

        neighborhood = np.nan_to_num(neighborhood)
        expected_neighborhood = np.nan_to_num(expected_neighborhood)

        try:
            test_result = mannwhitneyu(neighborhood, expected_neighborhood)
        except Exception:
            mask.append(False)
            continue
        pvalue = test_result[1]

        if np.isnan(pvalue):
            mask.append(False)
            continue
        if pvalue < pPValue:
            mask.append(True)
            pvalues.append(pvalue)
        else:
            mask.append(False)

    log.debug('MW-Test and p-value filtering...DONE')
    mask = np.array(mask)

    # Find other candidates within window size
    # accept only the candidate with the lowest pvalue

    # remove candidates which
    #  - do not full fill neighborhood size requirement
    #  - have a nan pvalue
    pCandidates = np.array(pCandidates)

    pCandidates = pCandidates[mask]
    log.debug('Number of candidates: {}'.format(len(pCandidates)))

    mask = []

    distances = euclidean_distances(pCandidates)
    # # call DBSCAN
    clusters = dbscan(X=distances, eps=pWindowSize,
                      metric='precomputed', min_samples=2, n_jobs=-1)[1]
    cluster_dict = {}
    for i, cluster in enumerate(clusters):
        if cluster == -1:
            mask.append(True)
            continue
        if cluster in cluster_dict:
            if pvalues[i] < cluster_dict[cluster][1]:
                mask[cluster_dict[cluster][0]] = False
                cluster_dict[cluster] = [i, pvalues[i]]
                mask.append(True)
            else:
                mask.append(False)

        else:
            cluster_dict[cluster] = [i, pvalues[i]]
            mask.append(True)

    # Remove values within the window size of other candidates
    mask = np.array(mask)
    pCandidates = pCandidates[mask]
    pvalues = np.array(pvalues)
    pvalues = pvalues[mask]

    # FDR
    pvalues_ = np.array([e if ~np.isnan(e) else 1 for e in pvalues])
    pvalues_ = np.sort(pvalues_)
    log.info('pvalues_ {}'.format(pvalues_))
    largest_p_i = -np.inf
    for i, p in enumerate(pvalues_):
        if p <= (pQValue * (i + 1) / len(pvalues_)):
            if p >= largest_p_i:
                largest_p_i = p
    pvalues_accepted = []
    log.info('largest_p_i {}'.format(largest_p_i))
    for i, candidate in enumerate(pCandidates):
        if pvalues[i] < largest_p_i:
            accepted_index.append(candidate)
            pvalues_accepted.append(pvalues[i])
    if len(accepted_index) == 0:
        log.info('No loops detected.')
        return None, None
    # remove duplicate elements
    for i, candidate in enumerate(accepted_index):
        if candidate[0] > candidate[1]:
            _tmp = candidate[0]
            candidate[0] = candidate[1]
            candidate[1] = _tmp
    duplicate_set_x = set([])
    duplicate_set_y = set([])

    delete_index = []
    for i, candidate in enumerate(accepted_index):
        if candidate[0] in duplicate_set_x and candidate[1] in duplicate_set_y:
            delete_index.append(i)
        else:
            duplicate_set_x.add(candidate[0])
            duplicate_set_y.add(candidate[1])

    delete_index = np.array(delete_index)
    accepted_index = np.array(accepted_index)
    pvalues_accepted = np.array(pvalues_accepted)
    accepted_index = np.delete(accepted_index, delete_index, axis=0)
    pvalues_accepted = np.delete(pvalues_accepted, delete_index, axis=0)

    return accepted_index, pvalues_accepted


def cluster_to_genome_position_mapping(pHicMatrix, pCandidates, pPValueList, pMaxLoopDistance):
    # mapping: chr_X, start, end, chr_Y, start, end, cluster_id
    mapped_cluster = []
    for i, candidate in enumerate(pCandidates):
        chr_x, start_x, end_x, _ = pHicMatrix.getBinPos(candidate[0])
        chr_y, start_y, end_y, _ = pHicMatrix.getBinPos(candidate[1])
        distance = abs(int(start_x) - int(start_y))
        if pMaxLoopDistance is not None and distance > pMaxLoopDistance:
            continue
        mapped_cluster.append(
            (chr_x, start_x, end_x, chr_y, start_y, end_y, pPValueList[i]))
    return mapped_cluster


def write_bedgraph(pLoops, pOutFileName, pStartRegion=None, pEndRegion=None):

    with open(pOutFileName, 'w') as fh:
        for loop_item in pLoops:
            if pStartRegion and pEndRegion:
                if loop_item[1] >= pStartRegion and loop_item[2] <= pEndRegion \
                        and loop_item[4] >= pStartRegion and loop_item[5] <= pEndRegion:
                    fh.write("%s\t%s\t%s\t%s\t%s\t%s\t%s\n" % loop_item)
            else:
                fh.write("%s\t%s\t%s\t%s\t%s\t%s\t%s\n" % loop_item)

    with open('loops_domains.bed', 'w') as fh:
        for loop_item in pLoops:
            if pStartRegion and pEndRegion:

                if loop_item[1] >= pStartRegion and loop_item[2] <= pEndRegion \
                        and loop_item[4] >= pStartRegion and loop_item[5] <= pEndRegion:
                    fh.write("{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format(loop_item[0], loop_item[1], loop_item[4], 1, loop_item[6],
                                                                           ".", loop_item[1], loop_item[4], "x,x,x"))
            else:
                fh.write("{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format(loop_item[0], loop_item[1], loop_item[4], 1, loop_item[6],
                                                                       ".", loop_item[1], loop_item[4], "x,x,x"))


def compute_loops(pHiCMatrix, pRegion, pArgs, pQueue=None):
    log.debug("Compute z-score matrix")
    z_score_matrix = compute_zscore_matrix(pHiCMatrix.matrix)
    if pArgs.zScoreMatrixName:

        matrixFileHandlerOutput = MatrixFileHandler(pFileType='cool')

        matrixFileHandlerOutput.set_matrix_variables(z_score_matrix, pHiCMatrix.cut_intervals, pHiCMatrix.nan_bins,
                                                     None, pHiCMatrix.distance_counts)
        matrixFileHandlerOutput.save(
            pRegion + '_' + pArgs.scoreMatrixName, pSymmetric=True, pApplyCorrection=False)

    candidates, pValueList = compute_long_range_contacts(pHiCMatrix, z_score_matrix, pArgs.zScoreThreshold,
                                                         pArgs.windowSize, pArgs.pValue, pArgs.qValue,
                                                         pArgs.peakInteractionsThreshold)
    if candidates is None:
        if pQueue is None:
            return None
        else:
            pQueue.put([None])
            return
    mapped_loops = cluster_to_genome_position_mapping(
        pHiCMatrix, candidates, pValueList, pArgs.maxLoopDistance)

    if pQueue is None:
        return mapped_loops
    else:
        pQueue.put([mapped_loops])
    return


def main():

    args = parse_arguments().parse_args()

    if args.region is not None and args.chromosomes is not None:
        log.error('Please choose either --region or --chromosomes.')
        exit(1)
    is_cooler = check_cooler(args.matrix)

    if args.region:
        chrom, region_start, region_end = translate_region(args.region)

        if is_cooler:
            hic_matrix = hm.hiCMatrix(
                pMatrixFile=args.matrix, pChrnameList=[args.region])
        else:
            hic_matrix = hm.hiCMatrix(args.matrix)
            hic_matrix.keepOnlyTheseChr([chrom])
        mapped_loops = compute_loops(hic_matrix, region_str, args)
        write_bedgraph(mapped_loops, args.outFileName, region_start, region_end)

    else:
        mapped_loops = []

        if not is_cooler:
            hic_matrix = hm.hiCMatrix(args.matrix)
            hic_matrix.keepOnlyTheseChr([chromosome])
            matrix = deepcopy(hic_matrix.matrix)
            cut_intervals = deepcopy(hic_matrix.cut_intervals)

        if args.chromosomes is None:
            # get all chromosomes from cooler file
            if not is_cooler:
                chromosomes_list = list(hic_matrix.chrBinBoundaries)
            else:
                chromosomes_list = cooler.Cooler(args.matrix).chromnames
        else:
            chromosomes_list = args.chromosomes

        if len(chromosomes_list) == 1:
            single_core = True
        else:
            single_core = False

        if single_core:
            for chromosome in chromosomes_list:
                if is_cooler:
                    hic_matrix = hm.hiCMatrix(pMatrixFile=args.matrix, pChrnameList=[chromosome])
                else:
                    hic_matrix.setMatrix(deepcopy(matrix), deepcopy(cut_intervals))
                    hic_matrix.keepOnlyTheseChr([chromosome])
                loops = compute_loops(hic_matrix, chromosome, args)
                if loops is not None:
                    mapped_loops.extend(loops)
        else:
            queue = [None] * args.threads
            process = [None] * args.threads
            all_data_processed = False
            all_threads_done = False
            thread_done = [False] * args.threads
            count_call_of_read_input = 0
            while not all_data_processed or not all_threads_done:
                for i in range(args.threads):
                    if queue[i] is None and not all_data_processed:

                        queue[i] = Queue()
                        thread_done[i] = False
                        if is_cooler:
                            hic_matrix = hm.hiCMatrix(pMatrixFile=args.matrix, pChrnameList=[chromosomes_list[count_call_of_read_input]])
                        else:
                            hic_matrix.setMatrix(deepcopy(matrix), deepcopy(cut_intervals))
                            hic_matrix.keepOnlyTheseChr([chromosomes_list[count_call_of_read_input]])

                        process[i] = Process(target=compute_loops, kwargs=dict(
                            pHiCMatrix=hic_matrix,
                            pRegion=chromosomes_list[count_call_of_read_input],
                            pArgs=args,
                            pQueue=queue[i]
                        ))
                        process[i].start()
                        if count_call_of_read_input < len(chromosomes_list):
                            count_call_of_read_input += 1
                        else:
                            all_data_processed = True
                    elif queue[i] is not None and not queue[i].empty():
                        result = queue[i].get()
                        if result[0] is not None:
                            mapped_loops.extend(result[0])

                        queue[i] = None
                        process[i].join()
                        process[i].terminate()
                        process[i] = None
                        thread_done[i] = True
                    elif all_data_processed and queue[i] is None:
                        thread_done[i] = True
                    else:
                        time.sleep(1)

                if all_data_processed:
                    all_threads_done = True
                    for thread in thread_done:
                        if not thread:
                            all_threads_done = False

        if len(mapped_loops) > 0:
            write_bedgraph(mapped_loops, args.outFileName)

    log.info("Number of detected loops: {}".format(len(mapped_loops)))