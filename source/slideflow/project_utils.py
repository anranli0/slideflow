"""Utility functions for Project, including functions for use in isolated processes."""
import os
import csv
import types
import slideflow as sf
import slideflow.io
import importlib.util
from os.path import join, exists
from slideflow.util import log

def _project_config(name='MyProject', annotations='./annotations.csv', dataset_config='./datasets.json',
                    sources='source1', models_dir='./models', eval_dir='./eval', mixed_precision=True,
                    batch_train_config='batch_train.tsv'):
    args = locals()
    args['slideflow_version'] = sf.__version__
    return args

def _heatmap_worker(slide, heatmap_args, kwargs):
    """Heatmap worker for :meth:`slideflow.Project.generate_heatmaps.`

    Any function loading a slide must be kept in an isolated process, as loading more than one slide
    in a single process causes instability / hangs. I suspect this is a libvips or openslide issue but
    I haven't been able to identify the root cause. Isolating processes when multiple slides are to be processed
    sequentially is a functional workaround, hence the process-isolated worker.
    """

    from slideflow.activations import Heatmap
    heatmap = Heatmap(slide,
                      model=heatmap_args.model,
                      stride_div=heatmap_args.stride_div,
                      roi_list=heatmap_args.roi_list,
                      roi_method=heatmap_args.roi_method,
                      buffer=heatmap_args.buffer,
                      normalizer=heatmap_args.normalizer,
                      normalizer_source=heatmap_args.normalizer_source,
                      batch_size=heatmap_args.batch_size,
                      num_threads=heatmap_args.num_threads)
    heatmap.save(heatmap_args.outdir, **kwargs)

def _train_worker(training_args, model_kwargs, training_kwargs, results_dict):
    """Internal function to execute model training in an isolated process."""

    import slideflow.model
    log.setLevel(training_args.verbosity)

    # Build a model using the slide list as input and the annotations dictionary as output labels
    trainer = sf.model.trainer_from_hp(training_args.hp,
                                        outdir=training_args.model_dir,
                                        labels=training_args.labels,
                                        patients=training_args.patients,
                                        slide_input=training_args.slide_input,
                                        **model_kwargs)

    results = trainer.train(training_args.train_dts,
                            training_args.val_dts,
                            pretrain=training_args.pretrain,
                            checkpoint=training_args.checkpoint,
                            multi_gpu=training_args.multi_gpu,
                            **training_kwargs)

    results_dict.update({model_kwargs['name']: results})

def get_validation_settings(**kwargs):
    """Returns a namespace of validation settings.

    Args:
        strategy (str): Validation dataset selection strategy. Defaults to 'k-fold'.
            Options include bootstrap, k-fold, k-fold-manual, k-fold-preserved-site, fixed, and none.
        k_fold (int): Total number of K if using K-fold validation. Defaults to 3.
        k (int): Iteration of K-fold to train, starting at 1. Defaults to None (training all k-folds).
        k_fold_header (str): Annotations file header column for manually specifying k-fold.
            Only used if validation strategy is 'k-fold-manual'. Defaults to None.
        fraction (float): Fraction of dataset to use for validation testing, if strategy is 'fixed'.
        source (str): Dataset source to use for validation. Defaults to None (same as training).
        annotations (str): Path to annotations file for validation dataset. Defaults to None (same as training).
        filters (dict): Filters dictionary to use for validation dataset. Defaults to None (same as training).

    """

    args_dict = {
        'strategy': 'k-fold',
        'k_fold': 3,
        'k': None,
        'k_fold_header': None,
        'fraction': None,
        'source': None,
        'annotations': None,
        'filters': None,
    }
    for k in kwargs:
        args_dict[k] = kwargs[k]
    args = types.SimpleNamespace(**args_dict)

    if (args.k_fold_header is None and args.strategy == 'k-fold-manual'):
        raise Exception("Must supply 'k_fold_header' if validation strategy is 'k-fold-manual'")

    return args

def add_source(name, slides, roi, tiles, tfrecords, path):
    """Adds a dataset source to a dataset configuration file.

    Args:
        name (str): Source name.
        slides (str): Path to directory containing slides.
        roi (str): Path to directory containing CSV ROIs.
        tiles (str): Path to directory in which to store extracted tiles.
        tfrecords (str): Path to directory in which to store TFRecords of extracted tiles.
        path (str): Path to dataset configuration file.
    """

    try:
        datasets_data = sf.util.load_json(path)
    except FileNotFoundError:
        datasets_data = {}
    datasets_data.update({name: {
        'slides': slides,
        'roi': roi,
        'tiles': tiles,
        'tfrecords': tfrecords,
    }})
    sf.util.write_json(datasets_data, path)
    log.info(f'Saved dataset source {name} to {path}')

def load_sources(path):
    """Loads datasets source configuration dictionaries from a given datasets.json file."""
    try:
        sources_data = sf.util.load_json(path)
        sources = list(sources_data.keys())
        sources.sort()
    except FileNotFoundError:
        sources_data = {}
        sources = []
    return sources_data, sources

def create_blank_train_config(filename):
    """Creates a TSV file with the batch training hyperparameter structure."""
    from slideflow.model import ModelParams
    with open(filename, 'w') as csv_outfile:
        writer = csv.writer(csv_outfile, delimiter='\t')
        # Create headers and first row
        header = ['model_name']
        firstrow = ['model1']
        default_hp = ModelParams()
        for arg in default_hp._get_args():
            header += [arg]
            firstrow += [getattr(default_hp, arg)]
        writer.writerow(header)
        writer.writerow(firstrow)

def interactive_project_setup(project_folder):
    """Guides user through project creation at the given folder, saving configuration to "settings.json"."""

    if not exists(project_folder): os.makedirs(project_folder)
    project = {}
    project['name'] = input('What is the project name? ')

    project['annotations'] = sf.util.path_input('Annotations file location [./annotations.csv] ',
                                                root=project_folder,
                                                default='./annotations.csv',
                                                filetype='csv',
                                                verify=False)

    # Dataset configuration
    project['dataset_config'] = sf.util.path_input('Dataset configuration file location [./datasets.json] ',
                                                    root=project_folder,
                                                    default='./datasets.json',
                                                    filetype='json',
                                                    verify=False)

    project['sources'] = []
    while not project['sources']:
        datasets_data, sources = load_sources(project['dataset_config'])

        print(sf.util.bold('Detected dataset sources:'))
        if not len(sources):
            print(' [None]')
        else:
            for i, name in enumerate(sources):
                print(f' {i+1}. {name}')
            print(f' {len(sources)+1}. ADD NEW')
            valid_source_choices = [str(l) for l in range(1, len(sources)+2)]
            selection = sf.util.choice_input(f'Which datasets should be used? ',
                                                    valid_choices=valid_source_choices,
                                                    multi_choice=True)

        if not len(sources) or str(len(sources)+1) in selection:
            # Create new dataset
            print(f"{sf.util.bold('Creating new dataset source')}")
            source_name = input('What is the dataset source name? ')
            source_slides = sf.util.path_input('Where are the slides stored? [./slides] ',
                                    root=project_folder, default='./slides', create_on_invalid=True)
            source_roi = sf.util.path_input('Where are the ROI files (CSV) stored? [./slides] ',
                                    root=project_folder, default='./slides', create_on_invalid=True)
            source_tiles = sf.util.path_input('Image tile storage location [./tiles] ',
                                    root=project_folder, default='./tiles', create_on_invalid=True)
            source_tfrecords = sf.util.path_input('TFRecord storage location [./tfrecord] ',
                                    root=project_folder, default='./tfrecord', create_on_invalid=True)

            add_source(name=source_name,
                        slides=source_slides,
                        roi=source_roi,
                        tiles=source_tiles,
                        tfrecords=source_tfrecords,
                        path=project['dataset_config'])

            print('Updated dataset configuration file.')
        else:
            try:
                project['sources'] = [sources[int(j)-1] for j in selection]
            except TypeError:
                print(f'Invalid selection: {selection}')
                continue

    project['models_dir'] = sf.util.path_input('Where should the saved models be stored? [./models] ',
                                                root=project_folder,
                                                default='./models',
                                                create_on_invalid=True)

    project['eval_dir'] = sf.util.path_input('Where should model evaluations be stored? [./eval] ',
                                                root=project_folder,
                                                default='./eval',
                                                create_on_invalid=True)

    project['mixed_precision'] = sf.util.yes_no_input('Use mixed precision? [Y/n] ', default='yes')
    project['batch_train_config'] = sf.util.path_input('Batch training TSV location [./batch_train.tsv] ',
                                                        root=project_folder,
                                                        default='./batch_train.tsv',
                                                        filetype='tsv',
                                                        verify=False)

    if not exists(project['batch_train_config']):
        print('Batch training file not found, creating blank')
        create_blank_train_config(project['batch_train_config'])

    # Save settings as relative paths
    settings = _project_config(**project)

    sf.util.write_json(settings, join(project_folder, 'settings.json'))

    # Write a sample actions.py file
    with open(join(os.path.dirname(os.path.realpath(__file__)), 'sample_actions.py'), 'r') as sample_file:
        sample_actions = sample_file.read()
        with open(os.path.join(project_folder, 'actions.py'), 'w') as actions_file:
            actions_file.write(sample_actions)
    log.info('Project configuration saved.')
    return settings
