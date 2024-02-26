#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# cems_data.py
#
##############################################################################
# REQUIRED MODULES
##############################################################################
import os
import logging
import urllib
import ftplib
import zipfile
import shutil
import time
import warnings

import pandas as pd
import requests

from electricitylci.globals import API_SLEEP
from electricitylci.globals import paths
from electricitylci.globals import output_dir
from electricitylci.globals import US_STATES


##############################################################################
# MODULE DOCUMENTATION
##############################################################################
__doc__ = """
Retrieve data from EPA CEMS daily zipped CSVs.

This module pulls data from EPA's published CSV files, which are utilized
in ampd_plant_emissions.py.

In the 2023 update, the workflow is to apply for an EPA Data API key

- https://www.epa.gov/power-sector/cam-api-portal#/api-key-signup

Then, run the following:

.. code: python

    >>> from electricitylci.cems_data import build_cems_df
    >>> df = build_cems_df(2016, use_api=True)

This will trigger the 48 lower states (plus DC) zip files to be downloaded.
Subsequent calls of ElectricityLCI will search for these local files before
triggering another API call, thus avoiding the API key input.

---

Copyright 2017 Catalyst Cooperative and the Climate Policy Initiative

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

Last edited:
    2024-02-26
"""


##############################################################################
# GLOBALS
##############################################################################
data_years = {
    'epacems': tuple(range(1995, 2021)),
}

epacems_columns_to_ignore = {
    "FACILITY_NAME",
    "SO2_RATE (lbs/mmBtu)",
    "SO2_RATE",
    "SO2_RATE_MEASURE_FLG",
    "CO2_RATE (tons/mmBtu)",
    "CO2_RATE",
    "CO2_RATE_MEASURE_FLG",
}

# NOTE: The op_date, op_hour, and op_time variables get converted to
# operating_date, operating_datetime and operating_time_interval in
# transform/epacems.py
epacems_csv_dtypes = {
    "STATE": str,
    "ORISPL_CODE": int,
    "UNITID": str,
    "OP_DATE": str,
    "OP_HOUR": int,
    "OP_TIME": float,
    "GLOAD (MW)": float,
    "GLOAD": float,
    "SLOAD (1000 lbs)": float,
    "SLOAD (1000lb/hr)": float,
    "SLOAD": float,
    "SO2_MASS (lbs)": float,
    "SO2_MASS": float,
    "SO2_MASS_MEASURE_FLG": str,
    "NOX_RATE (lbs/mmBtu)": float,
    "NOX_RATE": float,
    "NOX_RATE_MEASURE_FLG": str,
    "NOX_MASS (lbs)": float,
    "NOX_MASS": float,
    "NOX_MASS_MEASURE_FLG": str,
    "CO2_MASS (tons)": float,
    "CO2_MASS": float,
    "CO2_MASS_MEASURE_FLG": str,
    "HEAT_INPUT (mmBtu)": float,
    "HEAT_INPUT": float,
    "FAC_ID": int,
    "UNIT_ID": int,
}

epacems_rename_dict = {
    "STATE": "state",
    "ORISPL_CODE": "plant_id_eia",
    "UNITID": "unitid",
    "OP_DATE": "op_date",
    "OP_HOUR": "op_hour",
    "OP_TIME": "operating_time_hours",
    "GLOAD (MW)": "gross_load_mw",
    "GLOAD": "gross_load_mw",
    "SLOAD (1000 lbs)": "steam_load_1000_lbs",
    "SLOAD (1000lb/hr)": "steam_load_1000_lbs",
    "SLOAD": "steam_load_1000_lbs",
    "SO2_MASS (lbs)": "so2_mass_lbs",
    "SO2_MASS": "so2_mass_lbs",
    "SO2_MASS_MEASURE_FLG": "so2_mass_measurement_code",
    "NOX_RATE (lbs/mmBtu)": "nox_rate_lbs_mmbtu",
    "NOX_RATE": "nox_rate_lbs_mmbtu",
    "NOX_RATE_MEASURE_FLG": "nox_rate_measurement_code",
    "NOX_MASS (lbs)": "nox_mass_lbs",
    "NOX_MASS": "nox_mass_lbs",
    "NOX_MASS_MEASURE_FLG": "nox_mass_measurement_code",
    "CO2_MASS (tons)": "co2_mass_tons",
    "CO2_MASS": "co2_mass_tons",
    "CO2_MASS_MEASURE_FLG": "co2_mass_measurement_code",
    "HEAT_INPUT (mmBtu)": "heat_content_mmbtu",
    "HEAT_INPUT": "heat_content_mmbtu",
    "FAC_ID": "facility_id",
    "UNIT_ID": "unit_id_epa",
}

cems_states = {
    k: v for k, v in US_STATES.items() if v not in [
        'Alaska',
        'American Samoa',
        'Guam',
        'Hawaii',
        'Northern Mariana Islands',
        'National',
        'Puerto Rico',
        'Virgin Islands']
}

cems_col_names = {
    'GLOAD (MWh)': 'gross_load_mwh',
    'SO2_MASS (tons)': 'so2_mass_tons',
    'NOX_MASS (tons)': 'nox_mass_tons',
    'SUM_OP_TIME': 'sum_op_time',
    'COUNT_OP_TIME': 'count_op_time'
}


##############################################################################
# FUNCTIONS
##############################################################################
def _download_default(src_urls, tmp_files, allow_retry=True):
    """Download URLs to files. Designed to be called by `download` function.

    If the file cannot be downloaded, the program will issue a warning.

    Parameters
    ----------
    src_urls : list
        A list of the source URLs to download.
    tmp_files : list
        A list of the corresponding files to save.
    allow_retry : bool
        Should the function call itself again to retry the download?
        Default will try twice for a single file, or until all files fail.
    """
    assert len(src_urls) == len(tmp_files) > 0
    url_to_retry = []
    tmp_to_retry = []
    for src_url, tmp_file in zip(src_urls, tmp_files):
        try:
            outfile, _ = urllib.request.urlretrieve(src_url, filename=tmp_file)
        except urllib.error.URLError:
            url_to_retry.append(src_url)
            tmp_to_retry.append(tmp_to_retry)
    # Now retry failures recursively
    num_failed = len(url_to_retry)
    if num_failed > 0:
        if allow_retry and len(src_urls) == 1:
            # If there was only one URL and it failed, retry once.
            return _download_default(url_to_retry,
                                     tmp_to_retry,
                                     allow_retry=False
                                     )
        elif allow_retry and src_urls != url_to_retry:
            # If there were multiple URLs and at least one didn't fail,
            # keep retrying until all fail or all succeed.
            return _download_default(url_to_retry,
                                     tmp_to_retry,
                                     allow_retry=allow_retry
                                     )
        if url_to_retry == src_urls:
            err_msg = f"ERROR: Download failed for all {num_failed} URLs. Maybe the server is down?"
        if not allow_retry:
            err_msg = f"ERROR: Download failed for {num_failed} URLs and no more retries are allowed"
        warnings.warn(err_msg)


def _download_FTP(src_urls, tmp_files, allow_retry=True):
    """Add docstring."""
    assert len(src_urls) == len(tmp_files) > 0
    parsed_urls = [urllib.parse.urlparse(url) for url in src_urls]
    domains = {url.netloc for url in parsed_urls}
    within_domain_paths = [url.path for url in parsed_urls]
    if len(domains) > 1:
        # This should never be true, but it seems good to check
        raise NotImplementedError(
            "I don't yet know how to download from multiple domains")
    domain = domains.pop()
    ftp = ftplib.FTP(domain)
    login_result = ftp.login()
    assert login_result.startswith("230"), \
        f"Failed to login to {domain}: {login_result}"
    url_to_retry = []
    tmp_to_retry = []
    error_messages = []
    for path, tmp_file, src_url in zip(
            within_domain_paths, tmp_files, src_urls):
        with open(tmp_file, "wb") as f:
            try:
                ftp.retrbinary(f"RETR {path}", f.write)
            except ftplib.all_errors as e:
                error_messages.append(str(e))
                url_to_retry.append(src_url)
                tmp_to_retry.append(tmp_file)
    # Now retry failures recursively
    num_failed = len(url_to_retry)
    if num_failed > 0:
        if allow_retry and len(src_urls) == 1:
            # If there was only one URL and it failed, retry once.
            return _download_FTP(url_to_retry, tmp_to_retry, allow_retry=False)
        elif allow_retry and src_urls != url_to_retry:
            # If there were multiple URLs and at least one didn't fail,
            # keep retrying until all fail or all succeed.
            return _download_FTP(
                url_to_retry, tmp_to_retry, allow_retry=allow_retry)
        if url_to_retry == src_urls:
            err_msg = (
                f"Download failed for all {num_failed} URLs. " +
                "Maybe the server is down?\n" +
                "Here are the failure messages:\n " +
                " \n".join(error_messages)
            )
        if not allow_retry:
            err_msg = (
                f"Download failed for {num_failed} URLs and no more " +
                "retries are allowed.\n" +
                "Here are the failure messages:\n " +
                " \n".join(error_messages)
            )
        warnings.warn(err_msg)


def _write_cems_api(data, file_path):
    """Helper method for writing the API data frames to file.

    This is in support of repeated usage of ElectricityLCI to avoid
    running API calls (and inputting the API key) each and every time.

    Parameters
    ----------
    data : pandas.DataFrame
        A data frame with CEMS data as read from API and converted from JSON.
    file_path : str
        A path to the zip CSV file (e.g., as generated by :func:`path`).
        Warns if file already exists, as the default is to overwrite.

    Raises
    ------
    TypeError
        If other than data frame data object is received.
    """
    if os.path.exists(file_path):
        logging.warning("Overwriting existing CEMS CSV file!")

    if not isinstance(data, pd.DataFrame):
        raise TypeError("Expected pandas data frame, received %s" % type(data))

    file_dir = os.path.dirname(file_path)
    if not os.path.isdir(file_dir):
        logging.info("Creating output directory for CEMS data: %s" % file_dir)
        try:
            os.makedirs(file_dir, exist_ok=True)
        except Exception as e:
            logging.error("Failed to create folder, %s" % file_dir)
            logging.error("%s" % str(e))

    try:
        # Should infer zip compression from file extension.
        data.to_csv(file_path, index=False)
    except Exception as e:
        logging.error("Failed to write CEMS data to CSV: %s" % file_path)
        logging.error("%s" % str(e))
    else:
        logging.info("Saved CEMS data to file, %s" % file_path)


def assert_valid_param(source, year, qtr=None, state=None, check_month=None):
    """Assertions used for data validation.

    Note that this is not a proper use of data validation, as assertions can
    be disabled. See error handling.

    Parameters
    ----------
    source : str
        The source string (e.g., 'epacems').
    year : int
        The year (e.g., 1995--2021).
    qtr : int, optional
        The quarter (e.g., 1--4).
    state : str, optional
        The two-character state abbreviation (e.g, "PA").
    check_month : bool, optional
        Whether to check the state and quarter parameters.
    """
    assert source in ('epacems'), \
        f"Source '{source}' not found in valid data sources."
    assert source in data_years, \
        f"Source '{source}' not found in valid data years."
    assert source in {
            'epacems': 'ftp://newftp.epa.gov/dmdnload/emissions/daily/quarterly/'
            }, \
        f"Source '{source}' not found in valid base download URLs."
    assert year in data_years[source], \
        f"Year {year} is not valid for source {source}."
    if check_month is None:
        check_month = source == 'epacems'

    if source == 'epacems':
        valid_states = cems_states.keys()
    else:
        valid_states = US_STATES.keys()

    if check_month:
        assert qtr in range(1, 5), \
            f"Qtr {qtr} is not valid (must be 1-4)"
        assert state.upper() in valid_states, \
            f"State '{state}' is not valid. It must be a US state abbreviation."


def build_cems_df(year, use_api=False):
    """Build the CEMS data frame.

    Download the CEMS CSV zip files for each state for a given year,
    then open each one and append it to a pandas data frame, and
    aggregate the data by facility.

    Parameters
    ----------
    year : int
    use_api : bool, optional

    Returns
    -------
    pandas.DataFrame :
        A data frame with the annual CEMS data by facility.

    Examples
    --------
    >>> df = build_cems_df(2016)
    >>> list(df.columns)
    ['state',
     'plant_id_eia',
     'gross_load_mwh',
     'steam_load_1000_lbs',
     'so2_mass_tons',
     'nox_mass_tons',
     'co2_mass_tons',
     'heat_content_mmbtu']
    >>> len(df)
    1463
    >>> df.head()
      state  plant_id_eia  facility_id  ...  co2_mass_tons  heat_content_mmbtu
    0    AL             3            1  ...    8235782.477        1.055546e+08
    1    AL             7            3  ...     162134.726        2.734769e+06
    2    AL             8            4  ...    6140953.861        5.985333e+07
    3    AL            10            5  ...    1055231.747        1.354952e+07
    4    AL            26            6  ...    4981736.725        5.059296e+07

    Notes
    -----
    The same data can be accessed using EPA's CAMPD custom data download tool,
    which is available here:

    - https://campd.epa.gov/data/custom-data-download

    Set the following conditions in the query builder:

    - Data Type:

        - Data Type: Emissions
        - Data Subtype: Annual Emissions
        - Aggregation: Facility

    - Filters:

        - Time period: 2016

    Then click "Preview Data" and download the CSV.
    """
    states = cems_states.keys()

    if not use_api:
        update('epacems', year, states)
    raw_dfs = extract(
        epacems_years=[year],
        states=states,
        use_api=use_api
    )
    summary_df = process_cems_dfs(raw_dfs)
    return summary_df


def check_if_need_update(source, year, states, datadir, clobber, verbose):
    """
    Do we really need to download the requested data? Only case in which
    we don't have to do anything is when the downloaded file already exists
    and clobber is False.
    """
    paths = paths_for_year(source=source, year=year, states=states,
                           datadir=datadir)
    need_update = False
    message = None
    for path in paths:
        if os.path.exists(path):
            if clobber:
                message = f'{source} data for {year} already present, CLOBBERING.'
                need_update = True
            else:
                message = f'{source} data for {year} already present, skipping.'
        else:
            message = ''
            need_update = True
#    if verbose and message is not None:
    logging.info(message)
    return need_update


def download(source, year, states, datadir=paths.local_path, verbose=True):
    """Download the original data for the specified data source and year.

    Given a data source and the desired year of data, download the original
    data files from the appropriate federal website, and place them in a
    temporary directory within the data store. This function does not do any
    checking to see whether the file already exists, or needs to be updated,
    and does not do any of the organization of the datastore after download,
    it simply gets the requested file.

    Parameters
    ----------
    source : str
        The data source to retrieve. Must be one of: 'eia860',
        'eia923', 'ferc1', or 'epacems'.
    year : int
        The year of data that the returned path should pertain to.
        Must be within the range of valid data years, which is specified
        for each data source in pudl.constants.data_years. Note that for
        data (like EPA CEMS) that have multiple datasets per year, this
        function will download all the files for the specified year.
    datadir : str
        The path to the top level directory of the datastore.
    verbose : bool
        If True, see logging info messages about what's happening.

    Returns
    -------
    str
        The path to the local downloaded file.
    """
    assert_valid_param(source=source, year=year, check_month=False)

    tmp_dir = os.path.join(datadir, 'tmp')

    # Ensure that the temporary download directory exists:
    if not os.path.exists(tmp_dir):
        os.makedirs(tmp_dir)

    if source == 'epacems':
        src_urls = [source_url(source, year, qtr=qtr, state=state)
                    # For consistency, it's important that this is state, then
                    # month
                    for state in states
                    for qtr in range(1, 5)]
        tmp_files = [os.path.join(tmp_dir, os.path.basename(f))
                     for f in paths_for_year(source, year, states=states)]
    else:
        src_urls = [source_url(source, year)]
        tmp_files = [os.path.join(
            tmp_dir, os.path.basename(path(source, year)))]
    if verbose:
        if source != 'epacems':
            logging.info(
                f"Downloading {source} data for {year}...\n    {src_urls[0]}")
        else:
            logging.info(f"Downloading {source} data for {year}...")
    url_schemes = {urllib.parse.urlparse(url).scheme for url in src_urls}
    # Pass all the URLs at once, rather than looping here, because that way
    # we can use the same FTP connection for all of the src_urls
    # (without going all the way to a global FTP cache)
    if url_schemes == {"ftp"}:
        _download_FTP(src_urls, tmp_files)
    else:
        _download_default(src_urls, tmp_files)
    return tmp_files


def extract(epacems_years, states, use_api=True):
    """Extract the EPA CEMS hourly data.

    This function is the main function of this file. It returns a generator
    for extracted DataFrames.

    Parameters
    ----------
    epacems_years : list
        List of years.
    states : list
        List of states.
    use_api : bool, optional
        Option to by-pass the FTP download.
        Triggers input for API key, available for free at:
        https://www.epa.gov/power-sector/cam-api-portal#/api-key-signup

    Returns
    -------
    list
    """
    logging.info("Extracting EPA CEMS data...")
    dfs = []
    api_key = None

    for year in epacems_years:
        # The keys of the us_states dictionary are the state abbrevs
        for state in states:
            # Add API support
            if use_api:
                # HOTFIX: add local file support [2023-11-17; TWD]
                c_file = path("epacems", year=year, state=state)
                if os.path.exists(c_file):
                    logging.info(
                        "Found CEMS data file for %s %s" % (state, year))
                    tmp_df = pd.read_csv(c_file)
                else:
                    if api_key is None:
                        api_key = input("Enter EPA API key: ")
                    tmp_df = read_cems_api(api_key, year, state)

                # HOTFIX: don't add empty data frames
                records = len(tmp_df)
                logging.debug("%s %s: %d records" % (state, year, records))
                if records > 0:
                    dfs.append(tmp_df)
                time.sleep(API_SLEEP)
            else:
                # LEGACY CODE
                for qtr in range(1, 5):
                    filename = get_epacems_file(year, qtr, state)
                    logging.info(f"Reading {year} - {state} - qtr {qtr}")
                    dfs.append(read_cems_csv(filename))
    return dfs


def get_epacems_dir(year):
    """Data directory search for EPA CEMS hourly.

    LEGACY CODE

    Parameters
    ----------
    year : int
        The year that we're trying to read data for.

    Returns
    -------
    str
        Path to appropriate EPA CEMS data directory.
    """
    # These are the only years we've got...
    assert year in range(min(data_years['epacems']),
                         max(data_years['epacems']) + 1)

    return os.path.join(paths.local_path, 'epacems{}'.format(year))


def get_epacems_file(year, qtr, state):
    """Return the appropriate EPA CEMS zipfile for a given a year, month, and
    state.

    LEGACY CODE

    Parameters
    ----------
    year : int
        The year that we're trying to read data for.
    month : int
        The month we're trying to read data for.
    state : str
        The state we're trying to read data for.

    Returns
    -------
    str
        The path to EPA CEMS zipfiles for that year, month, and state.
    """
    state = state.lower()
    month = str(qtr)
    filename = f'epacems{year}{state}{qtr}.zip'
    full_path = os.path.join(get_epacems_dir(year), filename)
    assert os.path.isfile(full_path), (
        f"ERROR: Failed to find EPA CEMS file for {state}, {year}-{month}.\n" +
        f"Expected it here: {full_path}")
    return full_path


def organize(source, year, states, unzip=True,
             datadir=paths.local_path,
             verbose=False, no_download=False):
    """Put a downloaded original data file where it belongs in the datastore.

    Once we've downloaded an original file from the public website it lives on
    we need to put it where it belongs in the datastore. Optionally, we also
    unzip it and clean up the directory hierarchy that results from unzipping.

    Parameters
    ----------
    source : str
        The data source to retrieve. Must be one of: 'eia860',
        'eia923', 'ferc1', or 'epacems'.
    year : int
        The year of data that the returned path should pertain to.
        Must be within the range of valid data years, which is specified
        for each data source in pudl.constants.data_years.
    unzip : bool, optional
        If true, unzip the file once downloaded, and place the
        resulting data files where they ought to be in the datastore.
        Defaults to true.
    datadir : str, optional
        The path to the top level directory of the datastore.
        Defaults to local path.
    verbose : bool, optional
        If True, see logging info messages about what's happening.
        Defaults to false. Unused.
    no_download : bool, optional
        If True, the files were not downloaded in this run.
        Defaults to false.
    """
    assert source in ('epacems'), \
        "Source '{}' not found in valid data sources.".format(source)
    assert source in data_years, \
        "Source '{}' not found in valid data years.".format(source)
    assert source in {
            'epacems': 'ftp://newftp.epa.gov/dmdnload/emissions/daily/quarterly/'
            }, \
        "Source '{}' not found in valid base download URLs.".format(source)
    assert year in data_years[source], \
        "Year {} is not valid for source {}.".format(year, source)
    assert_valid_param(source=source, year=year, check_month=False)

    tmpdir = os.path.join(datadir, 'tmp')
    # For non-CEMS, the newfiles and destfiles lists will have length 1.
    newfiles = [os.path.join(tmpdir, os.path.basename(f))
                for f in paths_for_year(source, year, states)]
    destfiles = paths_for_year(
        source, year, states, file_=True, datadir=datadir)

    # If we've gotten to this point, we're wiping out the previous version of
    # the data for this source and year... so lets wipe it! Scary!
    destdir = path(source, year, file_=False, datadir=datadir)
    if not no_download:
        if os.path.exists(destdir):
            shutil.rmtree(destdir)
        # move the new file from wherever it is, to its rightful home.
        if not os.path.exists(destdir):
            os.makedirs(destdir)
        for newfile, destfile in zip(newfiles, destfiles):
            # paranoid safety check to make sure these files match...
            assert os.path.basename(newfile) == os.path.basename(destfile)
            shutil.move(newfile, destfile)  # works more cases than os.rename
    # If no_download is True, then we already did this rmtree and move
    # The last time this program ran.

    # If we're unzipping the downloaded file, then we may have some
    # reorganization to do. Currently all data sources will get unzipped,
    # except the CEMS, because they're really big and take up 92% less space.
    if(unzip and source != 'epacems'):
        # Unzip the downloaded file in its new home:
        zip_ref = zipfile.ZipFile(destfile, 'r')
        logging.info(f"unzipping {destfile}")
        zip_ref.extractall(destdir)
        zip_ref.close()
        # Most of the data sources can just be unzipped in place and be done
        # with it, but FERC Form 1 requires some special attention:
        # data source we're working with:
        if source == 'ferc1':
            topdirs = [os.path.join(destdir, td)
                       for td in ['UPLOADERS', 'FORMSADMIN']]
            for td in topdirs:
                if os.path.exists(td):
                    bottomdir = os.path.join(td, 'FORM1', 'working')
                    tomove = os.listdir(bottomdir)
                    for fn in tomove:
                        shutil.move(os.path.join(bottomdir, fn), destdir)
                    shutil.rmtree(td)


def path(source,
         year=0,
         qtr=None,
         state=None,
         file_=True,
         datadir=paths.local_path):
    """Construct a variety of local datastore paths for a given data source.

    PUDL expects the original data it ingests to be organized in a particular
    way. This function allows you to easily construct useful paths that refer
    to various parts of the data store, by specifying the data source you are
    interested in, and optionally the year of data you're seeking, as well as
    whether you want the originally downloaded files for that year, or the
    directory in which a given year's worth of data for a particular data
    source can be found.
    Note: if you change the default arguments here, you should also change them
    for paths_for_year()

    Parameters
    ----------
    source : str
        A string indicating which data source we are going to be
        downloading. Currently it must be one of the following:

        - 'ferc1'
        - 'eia923'
        - 'eia860'
        - 'epacems'

    year : int
        The year of data that the returned path should pertain to.
        Must be within the range of valid data years, which is specified
        for each data source in pudl.constants.data_years, unless year is
        set to zero, in which case only the top level directory for the
        data source specified in source is returned.
    file_ : bool
        If True, return the full path to the originally
        downloaded file specified by the data source and year.
        If file is true, year must not be set to zero, as a year is
        required to specify a particular downloaded file.
    datadir : str
        Path to the top level directory that contains the PUDL data store.

    Returns
    -------
    str :
        The path to the requested resource within the local PUDL datastore.
    """
    assert_valid_param(source=source, year=year, qtr=qtr, state=state,
                       check_month=False)

    if file_:
        assert year != 0, \
            "Non-zero year required to generate full datastore file path."

    if source == 'eia860':
        dstore_path = os.path.join(datadir, 'eia', 'form860')
        if year != 0:
            dstore_path = os.path.join(dstore_path, 'eia860{}'.format(year))
    elif source == 'eia861':
        dstore_path = os.path.join(datadir, 'eia', 'form861')
        if year != 0:
            dstore_path = os.path.join(dstore_path, 'eia861{}'.format(year))
    elif source == 'eia923':
        dstore_path = os.path.join(datadir, 'eia', 'form923')
        if year != 0:
            if year < 2008:
                prefix = 'f906920_'
            else:
                prefix = 'f923_'
            dstore_path = os.path.join(
                dstore_path, '{}{}'.format(prefix, year))
    elif source == 'ferc1':
        dstore_path = os.path.join(datadir, 'ferc', 'form1')
        if year != 0:
            dstore_path = os.path.join(dstore_path, 'f1_{}'.format(year))
    elif source == 'mshamines' and file_:
        dstore_path = os.path.join(datadir, 'msha')
        if year != 0:
            dstore_path = os.path.join(dstore_path, 'Mines.zip')
    elif source == 'mshaops':
        dstore_path = os.path.join(datadir, 'msha')
        if year != 0 and file_:
            dstore_path = os.path.join(
                dstore_path, 'ControllerOperatorHistory.zip')
    elif source == 'mshaprod' and file_:
        dstore_path = os.path.join(datadir, 'msha')
        if year != 0:
            dstore_path = os.path.join(dstore_path, 'MinesProdQuarterly.zip')
    elif (source == 'epacems'):
        dstore_path = paths.local_path
        if year != 0:
            dstore_path = os.path.join(dstore_path, 'epacems{}'.format(year))
    else:
        # we should never ever get here because of the assert statement.
        assert False, \
            "Bad data source '{}' requested.".format(source)

    # Handle month and state, if they're provided
    if qtr is None:
        qtr_str = ''
    else:
        qtr_str = str(qtr)
    if state is None:
        state_str = ''
    else:
        state_str = state.lower()
    # Current naming convention requires the name of the directory to which
    # an original data source is downloaded to be the same as the basename
    # of the file itself...
    if (file_ and source not in ['mshamines', 'mshaops', 'mshaprod']):
        basename = os.path.basename(dstore_path)
        # For all the non-CEMS data, state_str and month_str are '',
        # but this should work for other monthly data too.
        dstore_path = os.path.join(
            dstore_path, f"{basename}{state_str}{qtr_str}.zip"
        )
    return dstore_path


def paths_for_year(source,
                   year=0,
                   states=cems_states.keys(),
                   file_=True,
                   datadir=paths.local_path):
    """Get all the paths for a given source and year. See path() for details."""
    # TODO: I'm not sure this is the best construction, since it relies on
    # the order being the same here as in the url list comprehension
    if source == 'epacems':
        paths = [path(source=source, year=year, qtr=qtr, state=state,
                      file_=file_, datadir=datadir)
                 # For consistency, it's important that this is state, then
                 # month
                 for state in states
                 for qtr in range(1, 5)]
    else:
        paths = [path(source=source, year=year, file_=file_, datadir=datadir)]
    return paths


def process_cems_dfs(df_list):
    """Concatenate a list of quarterly state CEMS data frames and aggregate
    by facility.

    Parameters
    ----------
    df_list : list
        A list of pandas.DataFrame objects (see read_cems_csv).

    Returns
    -------
    pandas.DataFrame
        A concatenated and aggregated data frame.
        Columns include:

        - 'state' - two-letter state abbreviation
        - 'plant_id_eia' - the plant ID used elsewhere in eLCI
        - 'gross_load_mwh'
        - 'steam_load_1000_lbs'
        - 'so2_mass_tons'
        - 'nox_mass_tons'
        - 'co2_mass_tons'
        - 'heat_content_mmbtu'
    """
    df = pd.concat(df_list)
    df.rename(columns=cems_col_names, inplace=True)
    cols_to_sum = [
        'gross_load_mwh',
        'steam_load_1000_lbs',
        'so2_mass_tons',
        'nox_mass_tons',
        'co2_mass_tons',
        'heat_content_mmbtu'
    ]
    # HOTFIX: remove 'facility_id' from groupby
    new_df = df.groupby(
        by=['state', 'plant_id_eia'],
        group_keys=False,
        as_index=False
    )[cols_to_sum].sum()
    return new_df


def read_cems_api(api_key, year, state=None, force=False):
    """Read CEMS annual apportioned emissions from new EPA API.

    Method checks local directory for file existence and prioritizes
    reading from file before calling the API unless force is set to true.

    Parameters
    ----------
    api_key : str
        EPA data API key
    year : int
        Data year (e.g., 2016)
    state : str, optional
        Two-character state abbreviation (e.g., "VA"), by default None
    force : bool, optional
        Whether to force reading from API (rather than check for local copy).
        Defaults to false.

    Returns
    -------
    pandas.DataFrame
        CEMS data frame.

    Raises
    ------
    ValueError
        For missing API key.
    OSError
        For unexpected API errors.
    """
    # Use the annual apportioned emissions API URL:
    s_url = (
        "https://api.epa.gov/easey/streaming-services/emissions/"
        "apportioned/annual/by-facility"
    )
    # Keep column naming consistent with legacy code:
    c_map = {
        'stateCode': 'state',
        'facilityName': 'facility_name',
        'facilityId': 'plant_id_eia',
        'year': 'year',
        'grossLoad': 'gross_load_mwh',
        'steamLoad': 'steam_load_1000_lbs',
        'so2Mass': 'so2_mass_tons',
        'co2Mass': 'co2_mass_tons',
        'noxMass': 'nox_mass_tons',
        'heatInput': 'heat_content_mmbtu'
    }
    # Prepare the empty return dataframe
    tmp_df = pd.DataFrame(columns=list(c_map.values()))

    # HOTFIX: add local file checking [2023-11-17; TWD]
    c_file = path("epacems", year=year, state=state)
    if os.path.exists(c_file) and not force:
        logging.info("Found CEMS data file for %s %s" % (state, year))
        tmp_df = pd.read_csv(c_file)
    else:
        # Check that API key exists
        if api_key is None or api_key == "":
            raise ValueError("Missing API key!")

        # Prepare the API parameters
        params = {'api_key': api_key, 'year': year, 'stateCode': state}
        try:
            r = requests.get(s_url, params=params)
        except:
            raise OSError("Unexpected error during EPA data API call!")
        else:
            if r.ok:
                tmp_df = pd.DataFrame.from_dict(r.json()).rename(columns=c_map)
                _write_cems_api(tmp_df, c_file)
            else:
                # This catches incorrect API keys or bad parameters
                logging.warning(
                    "Failed to retrieve data for %s %s" % (state, year))

    return tmp_df


def read_cems_csv(filename):
    """Read one CEMS CSV file.

    Note that some columns are not read. See epacems_columns_to_ignores.

    LEGACY CODE

    Parameters
    ----------
    filename : str

    Returns
    -------
    pandas.DataFrame
    """
    df = pd.read_csv(
        filename,
        index_col=False,
        usecols=lambda col: col not in epacems_columns_to_ignore,
        dtype=epacems_csv_dtypes,
    ).rename(columns=epacems_rename_dict)
    return df


def source_url(source, year, qtr=None, state=None):
    """Construct a URL for the specified federal data source and year.

    LEGACY CODE

    Parameters
    ----------
    source : str
        A string indicating which data source we are going to be
        downloading. Currently it must be one of the following:

        - 'eia860'
        - 'eia861'
        - 'eia923'
        - 'ferc1'
        - 'mshamines'
        - 'mshaops'
        - 'mshaprod'
        - 'epacems'

    year : int
        The year for which data should be downloaded. Must be
        within the range of valid data years, which is specified for
        each data source in the pudl.constants module.
    month : int
        The month for which data should be downloaded.
        Only used for EPA CEMS.
    state : str
        The state for which data should be downloaded.
        Only used for EPA CEMS.

    Returns
    -------
    str
        A full URL from which the requested data may be obtained.
    """
    assert_valid_param(source=source, year=year, qtr=qtr, state=state)
    base_url = 'ftp://newftp.epa.gov/dmdnload/emissions/daily/quarterly/'
    download_url = '{base_url}/{year}/DLY_{year}{state}Q{qtr}.zip'.format(
            base_url=base_url, year=year,
            state=state.lower(), qtr=str(qtr)
    )
    return download_url


def update(source, year, states, clobber=False, unzip=True, verbose=True,
           datadir=paths.local_path, no_download=False):
    """Update the local datastore for the given source and year.

    If necessary, pull down a new copy of the data for the specified data
    source and year. If we already have the requested data, do nothing,
    unless clobber is True -- in which case remove the existing data and
    replace it with a freshly downloaded copy.

    Note that update_datastore.py runs this function in parallel, so files
    multiple sources and years may be in progress simultaneously.

    Parameters
    ----------
    source : str
        The data source to retrieve. Must be one of: 'eia860',
        'eia923', 'ferc1', or 'epacems'.
    year : int
        The year of data that the returned path should pertain to.
        Must be within the range of valid data years, which is specified
        for each data source in pudl.constants.data_years.
    clobber : bool, optional
        If true, replace existing copy of the requested data
        if we have it, with freshly downloaded data.
        Defaults to false.
    unzip : bool, optional
        If true, unzip the file once downloaded, and place the
        resulting data files where they ought to be in the datastore.
        EPA CEMS files will never be unzipped.
        Defaults to true.
    verbose : bool, optional
        If True, see logging info messages about what's happening.
        Defaults to true.
    datadir : str, optional
        The path to the top level directory of the datastore.
        Defaults to local path.
    no_download : bool, optional
        If True, don't download the files, only unzip ones
        that are already present. If False, do download the files. Either
        way, still obey the unzip and clobber settings. (unzip=False and
        no_download=True will do nothing.)
        Defaults to false.
    """
    need_update = check_if_need_update(source=source,
                                       year=year,
                                       states=states,
                                       datadir=datadir,
                                       clobber=clobber,
                                       verbose=verbose
                                       )
    if need_update:
        # Otherwise we're downloading:
        if not no_download:
            download(source, year, states, datadir=datadir, verbose=verbose)
        organize(source, year, states, unzip=unzip, datadir=datadir,
                 verbose=verbose, no_download=no_download)


##############################################################################
# MAIN
##############################################################################
if __name__ == '__main__':
    year = 2016
    df = build_cems_df(year)
    df.to_csv(f'{output_dir}/cems_emissions_{year}.csv')
