#from panoptes_client import *

import pandas as pd
import numpy as np
import os, sys
import pickle
import pdb
import argparse

import params
priors = params.Priors()
weighting = params.Weighting()

### Argument handling ###

argp = argparse.ArgumentParser()
argp.add_argument("-f", "--file-name", default='', type=str, help="File stem for output data")
argp.add_argument("-mp", "--multiproc", action="store_true", help="Specifies if you want to parallelize over multiple cores by splitting the images up. Default is off")
argp.add_argument("-nc", "--num-cores", default=None, type=int, help="Specify the number of cores that the retirement code will be parallelized over. Only used if multiproc is specified")
argp.add_argument("-i", "--index", default=None, type=int, help="Index which indicates the chunk of image files that retirement will be calculated for. Only used if multiproc is specified")

argp.add_argument("--min-label", default=1, type=int, help="Minimum number of citizen labels that an image must receive before it is retired. Default=1")
argp.add_argument("--max-label", default=50, type=int, help="Maximum number of citizen labels that an image must receive before it is retired as NOA. Default=50")
argp.add_argument("--ret-thresh", default=0.8, help="Retirement threshold that must be achieved to retire a particular class. Can be a float, or a 22-length vector of floats. Default = 0.8")
argp.add_argument("--prior", default='uniform', type=str, help="String indicating the prior choice for the subjects. Calls function from class params.py. Default=uniform")
argp.add_argument("--weighting", default='default', type=str, help="String indicating the weighting choice for the subjects. Calls function from class params.py. Default=default, where all users receive an equal weight")
args = argp.parse_args()


# Obtain number of classes from API
with open("../data/workflowDictSubjectSets.pkl","rb") as f:
    workflowDictSubjectSets = pickle.load(f)
classes = sorted(workflowDictSubjectSets[2117].keys())

# From ansers Dict determine number of classes
numClasses = len(classes)

# Flat retirement criteria #FIXME make this work for vector of thresholds
ret_thresh = float(args.ret_thresh)*np.ones(numClasses)

# Flat priors b/c we do not know what category the image is in #FIXME make this work for other defined priors
if args.prior == 'uniform':
    prior = priors.uniform(numClasses)

# Load classifications
print('\nreading classifications...')
classifications = pd.read_hdf('../data/classifications.hdf5')
classifications = classifications.loc[~(classifications.annotations_value_choiceINT == -1)]

# Load glitches
print('reading glitches...')
glitches = pd.read_hdf('../data/glitches.hdf5')
# filter glitches for only testing images
glitches = glitches.loc[glitches.ImageStatus != 'Training']
glitches['MLScore'] = glitches[classes].max(1)
glitches['MLLabel'] = glitches[classes].idxmax(1)

# Load confusion matrices
print('reading confusion matrices...')
conf_matrices1 = pd.read_hdf('../data/conf_matrices1.hdf5')
conf_matrices2 = pd.read_hdf('../data/conf_matrices2.hdf5')
conf_matrices3 = pd.read_hdf('../data/conf_matrices3.hdf5')
conf_matrices4 = pd.read_hdf('../data/conf_matrices4.hdf5')
conf_matrices5 = pd.read_hdf('../data/conf_matrices5.hdf5')
conf_matrices = pd.concat([conf_matrices1,conf_matrices2,conf_matrices3,conf_matrices4,conf_matrices5])

# Merge DBs
print('combining data...')
combined_data = classifications.merge(conf_matrices, on=['id','links_user'])
combined_data = combined_data.merge(glitches, on=['links_subjects', 'uniqueID'])

# Remove unnecessary columns from combined_data
col_list = ['id','uniqueID','links_subjects','links_workflow','links_user','MLScore','MLLabel','annotations_value_choiceINT','conf_matrix','metadata_finished_at']+sorted(workflowDictSubjectSets[2117].keys())
combined_data = combined_data[col_list]

# If a user has classifiied a glitch more than once, use earliest classification
combined_data.drop_duplicates(['links_subjects','links_user'], keep='first', inplace=True)

# Create imageDB
columnsForImageDB = sorted(workflowDictSubjectSets[2117].keys())
columnsForImageDB.extend(['uniqueID','links_subjects','MLScore','MLLabel','id'])
image_db = combined_data[columnsForImageDB].drop_duplicates(['links_subjects'])
image_db.set_index(['links_subjects'],inplace=True)
image_db['numLabel'] = 0
image_db['retired'] = 0
image_db['numClassifications'] = 0
image_db['finalScore'] = 0.0
image_db['finalLabel'] = ''
image_db['cumWeight'] = 0.0


def get_post_contribution(x):
    # NOTE: the variable 'x' is the subject link
    # find all classifications for a particular subject
    glitch = combined_data[combined_data.links_subjects==x]
    # only take classifications from registered users
    glitch = glitch[glitch.links_user != 0]
    # ensure each classification id has a confusion matrix
    matrices = combined_data[combined_data.id.isin(glitch.id)]
    glitch = glitch[glitch.id.isin(matrices.id)]
    # sort based on when the classification was made
    glitch = glitch.sort_values('metadata_finished_at')
    # counter to keep track of the weighting normalization, starts at 1.0 for machine
    weight_ctr = 1.0
    # track the contribution of each user towards retirement, initialize with ML score
    tracker = np.atleast_2d(glitch.iloc[0][classes].values)


    # Loop through all people that classified, or until retirement criteria are met
    for idx, person in enumerate(glitch.links_user):

        # Check for maximum number of labels
        if image_db.loc[x, 'numLabel'] > args.max_label:
            image_db.loc[x, 'numClassifications'] = args.max_label
            image_db.loc[x, 'finalScore'] = posterior.divide(weight_ctr).max()
            image_db.loc[x, 'finalLabel'] = classes[np.asarray(posterior.divide(weight_ctr)).argmax()]
            image_db.loc[x, 'tracks'] = [tracker]
            return

        # grab first person's annotation of the glitch
        classification = glitch[glitch.links_user == person]
        # save the correct confusion matrix
        matrix = matrices[matrices.id == int(classification.id)].conf_matrix.values[0]
        # find the row associated with the annotation the user made
        row = int(classification.annotations_value_choiceINT)
        # get the unweighted posterior contribution
        post_contribution = matrix/np.sum(matrix, axis=1)

        # If this user hasn't classified any golden images of this type, move on
        if np.isnan(post_contribution[row,:]).any():
            # if this is the last user in the group, save pertinent info
            if idx == len(glitch)-1:
                # first, make sure someone has contributed to the posterior for this image
                try:
                    posterior
                except NameError:
                    continue
                image_db.loc[x, 'numClassifications'] = image_db.loc[x, 'numLabel']
                image_db.loc[x, 'finalScore'] = posterior.divide(weight_ctr).max()
                image_db.loc[x, 'finalLabel'] = classes[np.asarray(posterior.divide(weight_ctr)).argmax()]
                image_db.loc[x, 'tracks'] = [tracker]
                return
            else:
                continue

        # calculate the weight of the user for this glitch classification
        weight = weighting.default(combined_data, person, x)
        # keep track of weighting counter for normalization purposes
        weight_ctr += weight
        # grab the posterior contribution for that class, weighted by classification weight
        posteriorToAdd = weight*post_contribution[row, :]
        # concatenate the new posterior contribution to tracker
        tracker = np.concatenate((tracker, np.asarray(posteriorToAdd)))
        # update image_db with the posterior contribution
        image_db.loc[x, classes] = image_db.loc[x, classes].add(np.asarray(posteriorToAdd).squeeze())
        # add 1 to numLabels for all images
        image_db.loc[x, 'numLabel'] = image_db.loc[x, 'numLabel'] + 1
        # get the total (unnormalized) posterior contribution at this point
        posterior = image_db.loc[x][classes]

        # Check for retirement threshold and minimum number of labels
        if ((posterior.divide(weight_ctr) > ret_thresh).any() and image_db.loc[x, 'numLabel'] >= args.min_label):
            # save pertinent info of the retired image
            image_db.loc[x, classes] = image_db.loc[x, classes].divide(weight_ctr)
            image_db.loc[x, 'numClassifications'] = image_db.loc[x, 'numLabel']
            image_db.loc[x, 'finalScore'] = posterior.divide(weight_ctr).max()
            image_db.loc[x, 'finalLabel'] = classes[np.asarray(posterior.divide(weight_ctr)).argmax()]
            image_db.loc[x, 'retired'] = 1
            image_db.loc[x, 'cumWeight'] = weight_ctr
            image_db.loc[x, 'tracks'] = [tracker]
            return

       # If all people have been accounted for and image not retired, save info to image_db and tracks
        if idx == len(glitch.links_user)-1:
            image_db.loc[x, 'numClassifications'] = image_db.loc[x, 'numLabel']
            image_db.loc[x, 'finalScore'] = posterior.divide(weight_ctr).max()
            image_db.loc[x, 'finalLabel'] = classes[np.asarray(posterior.divide(weight_ctr)).argmax()]
            image_db.loc[x, 'tracks'] = [tracker]
            return


print('determining retired images...')
# sort data based on subjects number
subjects = combined_data.links_subjects.unique()
subjects.sort()

# implementation for multiprocessing
if args.multiproc:
    breakdown = np.linspace(0,len(subjects),args.num_cores+1)
    subjects = subjects[int(np.floor(breakdown[args.index-1])):int(np.floor(breakdown[args.index]))]
    image_db = image_db.loc[subjects]

# do the loop
for idx, g in enumerate(subjects):
    get_post_contribution(g)
    if idx%100 == 0:
        print('%.2f%% complete' % (100*float(idx)/len(subjects)))

# save image and retirement data as pickles
if args.multiproc:
    image_db.to_pickle('../output/imageDB_'+args.file_name+str(args.index)+'.pkl')
else:
    image_db.to_pickle('../output/imageDB_'+args.file_name+'.pkl')
