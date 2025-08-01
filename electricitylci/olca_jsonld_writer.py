#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# olca_jsonld_writer.py
#
##############################################################################
# REQUIRED MODULES
##############################################################################
import datetime
import io
import json
import logging
import math
import os
import re
import uuid
from zipfile import ZipFile

import fedelemflowlist
import olca_schema as o
import olca_schema.units as o_units
import olca_schema.zipio as zipio
import pytz
import requests

from electricitylci.globals import paths
from electricitylci.globals import elci_version as VERSION
from electricitylci.utils import check_output_dir


##############################################################################
# MODULE DOCUMENTATION
##############################################################################
__doc__ = """This module provides methods that support the writing of
olca-schema formatted dictionaries to JSON-LD project files for openLCA.

Unit groups and flow properties are based on the Federal Elementary Flow List
found on the LCA Commons (https://www.lcacommons.gov/lca-collaboration/). This
was chosen because the UUIDs are consistent with GreenDelta's openLCA schema
and provide the full metadata where olca-schema Python package provides only
Ref objects. This could be avoided if opting for 'Units and flow properties'
is ticked when creating a new openLCA database.

References:

    -   GreenDelta openLCA schema

        -   https://greendelta.github.io/olca-schema/
        -   https://github.com/GreenDelta/olca-schema

Changelog:

    -   Remove annotations in method type hints & replace with proper doc
        strings.
    -   Fix and simplify the _val method; properly returns first key value
    -   Simplify the _unit method (see olca_schema.units)
    -   Remove the _compartment method (not necessary in new schema)
    -   Overhaul the data quality methods.
    -   Utilize datetime package in _format_date
    -   New methods for getting current date and year
    -   New methods for checking valid year and UUID
    -   New flow property generator based on olca-schema.units module
    -   Fix locations that are just strings (not a data dictionary)
    -   Fix UUID validation for nan
    -   Add openLCA's unit group and flow property lists based on the
        Federal LCA Commons's Federal Elementary Flow List
    -   New method for reading JSON-LD that fixes repeated .json entries
    -   New save method that checks for existing JSON-LD, extracts its data,
        updates with new data, then re-zips (proper handling of zip archive).
    -   New build product systems method
    -   Fix memory leak in ``_make_product_system`` call to
        ``_build_supply_chain``
    -   Add missing 'Electricity; at user; consumption mix - US - US' process
        to product system generation
    -   Correct emissions labeled as resource
    -   Add flow metadata from USEPA's fedelemflowlist
    -   Speed up fedelemflowlist writing
    -   Move Federal LCA Commons' JSON assets to local data store
    -   Add EPA's DQI pedigree matrices to JSON-LD
    -   Fix removal of untracked flows (new :func:`rm_untracked_flows`)
    -   Add two more corrections to :func:`clean_json`

Last edited:
    2025-06-10
"""
__all__ = [
    "build_product_systems",
    "check_exchanges",
    "clean_json",
    "write",
]


##############################################################################
# FUNCTIONS
##############################################################################
def build_product_systems(file_path, elci_config):
    """Generates product systems for electricity at user consumption mixes.

    Parameters
    ----------
    file_path : str
        A file path to an existing JSON-LD file with process data saved.
    elci_config : str
        The model configuration used to make the inventory (e.g., "ELCI_1")

    Notes
    -----
    -   This method checks process exchange amounts for NaNs, which cause
        openLCA to crash.
    -   This method overwrites the existing JSON-LD with the new product
        systems.
    """
    try:
        # Read all JSON-LD data in order to overwrite.
        data = _read_jsonld(file_path, _root_entity_dict())
    except OSError:
        logging.warning("Failed to read JSON-LD file, %s" % file_path)
    else:
        check_exchanges(data['Process']['objs'])
        logging.info("Building product systems in JSON-LD")

    # Find all processes for 'at user' consumption mixes
    q1 = re.compile("^Electricity; at user; consumption mix - (.*) - BA$")
    q2 = re.compile("^Electricity; at user; consumption mix - (.*) - FERC$")
    q3 = re.compile("^Electricity; at user; consumption mix - US - US$")
    r1 = _match_process_names(data['Process']['objs'], q1)
    r2 = _match_process_names(data['Process']['objs'], q2)
    r3 = _match_process_names(data['Process']['objs'], q3)
    r = r1 + r2 + r3
    logging.info("Processing %d product systems" % len(r))

    # Create a common description text
    t_now = datetime.datetime.now()
    d_txt = (
        "This product system was created in openLCA "
        "by linking default providers. "
        "The processes were generated by ElectricityLCI "
        "(https://github.com/USEPA/ElectricityLCI) "
        f"version {VERSION} using "
        f"the {elci_config} configuration. "
        f"Created: {t_now.isoformat()}."
    )

    for pid in r:
        p_idx = data['Process']['ids'].index(pid)
        p_obj = data['Process']['objs'][p_idx]
        ps_obj = _make_product_system(file_path, p_obj, d_txt)

        # Update master data dictionary
        data['ProductSystem']['objs'].append(ps_obj)
        data['ProductSystem']['ids'].append(ps_obj.id)
        logging.debug("Created %s" % ps_obj.name)

    # Overwrite JSON-LD
    _save_to_json(file_path, data)


def check_exchanges(p_list):
    """Iterate over process exchanges and log as error when an amount is nan.

    This occurrence causes openLCA to crash on upload of JSON-LD.

    Parameters
    ----------
    p_list : list
        A list of olca-schema.Process objects.
    """
    logging.info("Checking process exchange amounts for NaNs")
    for p in p_list:
        for ex in p.exchanges:
            if ex.amount != ex.amount:
                e_str = "output"
                if ex.is_input:
                    e_str = "input"
                logging.error(
                    "Found nan for '%s' in %s exchange %d of process '%s'" % (
                        ex.flow.name,
                        e_str,
                        ex.internal_id,
                        p.name,
                ))


def clean_json(file_path):
    """Perform the following clean-up steps on JSON-LD.

    1.  Remove zero-valued product flows from processes.
    2.  Consecutively number exchange internal IDs.
    3.  Update flows with FEDEFL metadata.
    4.  Re-label heat inputs as elementary flow.
    5.  Fix compartment path 'Elementary Flows/Elementary Flows'
        associated with the resources 'Heat' and 'Water, reclaimed'
    6.  Fix compartment for two product flows: 'Light fuel oil' and
        'Ammonium nitrate' from the coal model.

    Parameters
    ----------
    file_path : str
        A file path to an existing JSON-LD zip archive.
    """
    try:
        data = _read_jsonld(file_path, _root_entity_dict())
    except OSError:
        logging.warning("Failed to read JSON-LD file, %s" % file_path)
    else:
        logging.info("Cleaning JSON-LD")

        # Pull flows from each process's exchange list; remove zero product
        # flows along the way.
        # https://github.com/USEPA/ElectricityLCI/issues/217
        e_list = []
        for p in data["Process"]['objs']:
            for e in p.exchanges:
                # Get the flow object
                fid = data["Flow"]['ids'].index(e.flow.id)
                f_obj = data["Flow"]['objs'][fid]

                # Remove if flow is a product flow with zero exchange value
                # NOTE: don't add as an exchange flow!
                if e.amount == 0 and (
                        f_obj.flow_type != o.FlowType.ELEMENTARY_FLOW):
                    logging.debug(
                        "Removing zero product flow, %s, from %s" % (
                            e.flow.name, p.name))
                    p.exchanges.remove(e)
                else:
                    # Add to list of tracked exchanges
                    e_list.append(e.flow.id)

                # Check if output exchange is labeled as a resource flow
                # https://github.com/USEPA/ElectricityLCI/issues/233
                if not e.is_input and 'resource' in f_obj.category.lower():
                    logging.warning(
                        "Fixing resource flow in output exchange! "
                        "'%s' in %s (%s)" % (f_obj.name, p.name, p.id))
                    # HOTFIX: remove troublesome exchange, fix meta & re-add:
                    p.exchanges.remove(e)
                    e.is_input = True
                    e.description = "mislabeled resources"
                    p.exchanges.append(e)

                # Correct heat resource flows;
                # https://github.com/USEPA/ElectricityLCI/issues/293
                if e.is_input and e.flow.name == 'Heat':
                    # The new FEDEFL heat resource flow
                    h_flow = _heat_elem_flow()
                    # Add elementary flow if missing
                    if h_flow.id not in data['Flow']['ids']:
                        data['Flow']['ids'].append(h_flow.id)
                        data['Flow']['objs'].append(h_flow)
                    # Add new heat to tracked list (if not already)
                    if h_flow.id not in e_list:
                        e_list.append(h_flow.id)
                    # Remove the defunct heat flow from tracked list
                    if e.flow.id in e_list:
                        e_fid = e_list.index(e.flow.id)
                        e_list.pop(e_fid)
                    # Remove old exchange, add new with updated description.
                    p.exchanges.remove(e)
                    e.flow = h_flow.to_ref()
                    if e.description:
                        e.description = "mapped to FEDEFL; " + e.description
                    else:
                        e.description = "mapped to FEDEFL"
                    p.exchanges.append(e)

                # Correct double Elementary Flows category
                # https://github.com/USEPA/ElectricityLCI/issues/149
                if f_obj.category.startswith(
                        "Elementary flows/Elementary Flows"):
                    logging.warning(
                        "Fixing duplicate Elementary flows category "
                        "for '%s'" % f_obj.name)
                    f_obj.category = f_obj.category.replace(
                        "/Elementary Flows/", "/")

                # Map third-party technosphere flows to NAICS
                # https://github.com/USEPA/ElectricityLCI/issues/149
                # NOTE: this overwrite breaks the reproducibility of the
                # UUIDs for these two flows
                tech_cat = "Technosphere Flows"
                pri_cat = "31-33: Manufacturing"
                lfo_cat = "3241: Petroleum and Coal Products Manufacturing"
                an_cat = (
                    "3253: Pesticide, Fertilizer, and Other Agricultural "
                    "Chemical Manufacturing"
                )
                if (f_obj.name == "Light fuel oil") and (
                    f_obj.flow_type == o.FlowType.PRODUCT_FLOW) and not (
                        pri_cat in f_obj.category):
                    logging.warning(
                        "Mapping 'Light fuel oil' technosphere flow to "
                        "NAICS 3241")
                    f_obj.category = "/".join([tech_cat, pri_cat, lfo_cat])
                elif (f_obj.name == "Ammonium nitrate") and (
                    f_obj.flow_type == o.FlowType.PRODUCT_FLOW) and not (
                        pri_cat in f_obj.category):
                    logging.warning(
                        "Mapping 'Ammonium nitrate' technosphere flow to "
                        "NAICS 3253")
                    f_obj.category = "/".join([tech_cat, pri_cat, an_cat])

            # Loop through exchanges a second time and re-number their
            # internal IDs to a consecutive order.
            p.last_internal_id = 0
            for e in p.exchanges:
                p.last_internal_id += 1
                e.internal_id = p.last_internal_id

        # Sort unique values to speed up search
        e_list = sorted(list(set(e_list)))

        # Overwrite
        _save_to_json(file_path, data)


def write(processes, file_path, to_save=True):
    """Write a process dictionary as a olca-schema zip file to the given path.

    Note that a process has several root entity types associated with it,
    namely:

    - 'Actor',
    - 'Currency'
    - 'DQSystem'
    - 'EPD'
    - 'Flow'
    - 'FlowProperty'
    - 'ImpactCategory'
    - 'ImpactMethod'
    - 'Location'
    - 'Parameter'
    - 'Process'
    - 'ProductSystem'
    - 'Project'
    - 'Result'
    - 'SocialIndicator'
    - 'Source'
    - 'UnitGroup'

    and each of which need to be tracked and stored in the JSON-LD. Changes
    to any root entities triggers a modification of the JSON-LD file. Because
    the JSON-LD is a zip archive, the uuid.json files within it cannot be
    edited without first extracting from zip, editing, then re-zipping.

    To allow for editing of root entities (assuming any new information is
    good information), and because this method is called (again and again) in
    electricity.main, in each method call, the JSON-LD file is examined, its
    data extracted and updated with the latest data, and re-zipped to the same
    JSON-LD archive (deleting the old version in the process).

    The same methodology is adopted in NetlOlca Python class for interfacing
    with openLCA v2 projects. This is the way.

    Parameters
    ----------
    processes : dict
        OLCA schema dictionaries (e.g. Process).
    file_path : str
        A path to a zip file where the JSON-LD will be written.
    to_save : bool
        Whether this method should write the JSON-LD to zip file.

    Returns
    -------
    dict
        Original processes dictionary updated.

    Notes
    -----
    GreenDelta, olca-schema, Python tests (e.g., test_zipio.py).
    Online: https://github.com/GreenDelta/olca-schema/
    """
    # Make sure output folder exists
    file_dir = os.path.dirname(file_path)
    if not os.path.exists(file_dir):
        logging.info("Creating folder, '%s'" % file_dir)
        os.makedirs(file_dir)

    # Initialize root entity mapper (i.e., dictionary), which includes all
    # entities already written to the JSON-LD file, or simply GreenDelta's
    # FlowProperties and UnitGroups.
    spec_map = _init_root_entities(file_path)

    for p_key in processes.keys():
        # Pull the process dictionary
        d_vals = processes[p_key]

        # Create new process object and find quantitative reference exchange
        logging.info("Generating process for %s" % p_key)
        p, spec_map, e = _process(d_vals, spec_map)
        spec_map['Process']['ids'].append(p.id)
        spec_map['Process']['objs'].append(p)

        # Update the process dictionary and add UUID and reference details
        processes[p_key].update(p.to_dict())
        processes[p_key]['uuid'] = p.id
        if e is not None and isinstance(e, o.Exchange):
            try:
                processes[p_key]['q_reference_name'] = e.flow.name
                processes[p_key]['q_reference_id'] = e.flow.id
                processes[p_key]['q_reference_cat'] = e.flow.category
                processes[p_key]['q_reference_unit'] = e.unit.name
            except Exception as exception:
                logging.warning(
                    "Unexpected error when accessing quantitative "
                    "reference exchange for '%s'. %s" % (
                        p_key, str(exception)
                    )
                )

    # Write to JSON-LD zip format
    if to_save:
        logging.info("Saving to '%s'" % file_path)
        _save_to_json(file_path, spec_map)

    return processes


def _actor(name, dict_s):
    """Generate a reference object to an actor.

    Parameters
    ----------
    name : str
        Actor name
    dict_s : dict
        Dictionary with created olca schema root entities.

    Returns
    -------
    tuple
        olca_schema.Ref : Reference object to an Actor.
        dict : The updated root entities dictionary, ``dict_s``.
    """
    # Skip unnamed or missing actors.
    if not isinstance(name, str) or name == '':
        return None

    # Generate a standard UUID based on actor name:
    uid = _uid(o.ModelType.ACTOR, name)

    # Check to see if Actor is already recorded.
    # If so, retrieve it; otherwise, make new and record it!
    if uid in dict_s['Actor']['ids']:
        idx = dict_s['Actor']['ids'].index(uid)
        actor = dict_s['Actor']['objs'][idx]
        logging.debug("Found existing actor, %s" % actor.name)
    else:
        logging.debug("Creating new actor entity for '%s'" % name)
        actor = o.Actor()
        actor.id = uid
        actor.name = name
        dict_s['Actor']['ids'].append(uid)
        dict_s['Actor']['objs'].append(actor)

    return (actor.to_ref(), dict_s)


def _add_fed_commons(spec_map):
    """Append openLCA unit groups, flow properties, and DQI to a spec map
    dictionary.

    Parameters
    ----------
    spec_map : dict
        A dictionary of openLCA root entities.
        Requires 'UnitGroup' key dictionary value with keys, 'objs' and 'ids'.

    Returns
    -------
    dict
        The same spec map with UnitGroup, FlowProperty, DQSystem, and Source
        objects and UUIDs appended appropriately.

    Raises
    ------
    TypeError
        If the spec map is not a dictionary.
    KeyError
        If the spec map is missing the required key(s).
    """
    # A little bit of error handling :)
    if not isinstance(spec_map, dict):
        raise TypeError("Expected a dictionary, received %s" % type(spec_map))
    if "UnitGroup" not in spec_map.keys():
        raise KeyError("Failed to find required UnitGroup key!")
    if "FlowProperty" not in spec_map.keys():
        raise KeyError("Failed to find required FlowProperty key!")
    if "DQSystem" not in spec_map.keys():
        raise KeyError("Failed to find required DQSystem key!")
    if "Source" not in spec_map.keys():
        raise KeyError("Failed to find required Source key!")

    u_list, p_list = _read_fedefl()
    for u_obj in u_list:
        spec_map['UnitGroup']['objs'].append(u_obj)
        spec_map['UnitGroup']['ids'].append(u_obj.id)
    for p_obj in p_list:
        spec_map['FlowProperty']['objs'].append(p_obj)
        spec_map['FlowProperty']['ids'].append(p_obj.id)

    d_list, s_list =  _read_fedcore()
    for d_obj in d_list:
        spec_map['DQSystem']['objs'].append(d_obj)
        spec_map['DQSystem']['ids'].append(d_obj.id)
    for s_obj in s_list:
        spec_map['Source']['objs'].append(s_obj)
        spec_map['Source']['ids'].append(s_obj.id)

    return spec_map


def _archive_json(data_list, file_path):
    """Write a list of dictionaries to a JSON file.

    Parameters
    ----------
    data_list : list
        A list of dictionaries.
    file_path : str
        A valid filepath to be written to (CAUTION: overwrites existing data)
    """
    logging.debug("Writing %d items to %s" % (len(data_list), file_path))
    out_str = ",".join([json.dumps(x.to_dict()) for x in data_list])
    out_str = "[%s]" % out_str
    with open(file_path, 'w') as f:
        f.write(out_str)


def _build_supply_chain(zh, pid, e_list=[], p_list=[]):
    """Populate the processes and process links lists based on the
    default providers assigned to a given process.

    Parameters
    ----------
    zh : zipio.ZipReader
        File handle to an opened JSON-LD archive.
    pid : str
        A process's universally unique identifier.
    e_list : list, optional
        A list of ProcessLinks, by default [].
    p_list : list, optional
        A list of Process UUIDs, by default [].

    Returns
    -------
    tuple
        A tuple of length two: list of ProcessLinks and a list of
        Process UUIDs.

    Notes
    -----
    1.  This method does not apply any standardization for UUID generation.
        It is arbitrarily generated by olca_schema class initialization.
    2.  The methods here are heavily based on those from NETL's NetlOlca class
        currently under development by KeyLogic, here:
        https://github.com/KeyLogicLCA/netlolca
    """
    # Pull process object from JSON-LD and add to the processes list.
    p_obj = zh.read(o.Process, pid)
    if p_obj and (pid not in p_list):
        logging.debug("Adding process, '%s'" % p_obj.name)
        p_list.append(pid)
        # Iterate over input exchanges w/ default providers.
        for ex in p_obj.exchanges:
            if ex.is_input and (ex.default_provider is not None):
                # NOTE: should id be generated?
                p_link = o.ProcessLink(
                    exchange=o.ExchangeRef(internal_id=ex.internal_id),
                    flow=ex.flow,
                    process=_make_process_ref(p_obj),
                    provider=ex.default_provider,
                )
                e_list.append(p_link)

                # Build supply chain for default provider, which should
                # itself be a process.
                e_list, p_list = _build_supply_chain(
                    zh, ex.default_provider.id, e_list, p_list)

    return (e_list, p_list)


def _check_source_year(s_year):
    """Checks value as valid year.

    Implements two basic checks:

    1. Type check (i.e., integer)
    2. Range check (values between 0 and current year + 1)

    Parameters
    ----------
    s_year : str, int
        A year.

    Returns
    -------
    int
        Year.
        Defaults to current year for invalid parameters.
    """
    try:
        int(s_year)
    except:
        logging.warning("Invalid source year, defaulting to current year.")
        r_year = _current_year()
    else:
        r_year = int(s_year)
        if (r_year < 0) or (r_year > _current_year() + 1):
            logging.warning(
                "Source year out of range! Default to current year.")
            r_year = _current_year()
    finally:
        return r_year


def _current_time():
    """Return a ISO-formatted time stamp for right now.

    Returns
    -------
    str
        Time stamp in ISO format.

    Examples
    --------
    >>> _current_time()
    '2023-10-25T21:06:19.200675+00:00'
    """
    return datetime.datetime.now(pytz.utc).isoformat()


def _current_year():
    """Return today's calendar year.

    Returns
    -------
    int
        Year.
    """
    return datetime.datetime.now(pytz.utc).year


def _dq_entry(dict_d):
    """Return the data quality entry vector.

    Searches both the process dictionary and processDocumentation dictionary
    (if available) for the dqEntry value.

    Parameters
    ----------
    dict_d : dict
        Process or processDocumentation dictionary.

    Returns
    -------
    str
        Data quality entry vector, for example: '(1;3;2;5)'.
    """
    return _format_dq_entry(_find_dq(dict_d, 'dqEntry'))


def _dq_system(dict_d, dict_s, dq_type):
    """Generate reference to olca-schema DQSystem for a given process.

    Parameters
    ----------
    dict_d : dict
        Process or processDocumentation data dictionary.
    dict_s : dict
        Dictionary storing olca-schema root entities.
    dq_type : str
        The data quality type. Valid values include 'dqSystem' and
        'exchangeDqSystem'.

    Returns
    -------
    tuple
        olca_schema.Ref : Reference to DQSystem object.
        dict : Updated dictionary storing olca-schema root entities.

    Raises
    ------
    ValueError
        If invalid dq_type received.

    Notes
    ----
    This method introduces a standard for defining new DQSystems;
    however, this should never occur, given the definition provided in
    process_dictionary_writer.py.

    For more information on the DQSystem attribute for a Process, see
    https://greendelta.github.io/olca-schema/classes/Process.html#dqsystem
    """
    if dq_type not in ['dqSystem', 'exchangeDqSystem']:
        raise ValueError(
            "Expected 'dqSystem' or 'exchangeDqSystem', "
            "received '%s'" % dq_type
        )
    # Pre-define description text.
    dq_desc = "A process data quality system entry."
    if dq_type == 'exchangeDqSystem':
        dq_desc = "An exchange data quality system entry."

    dq = _find_dq(dict_d, dq_type)
    if not isinstance(dq, dict):
        return (None, dict_s)

    dq_id = _val(dq, '@id')
    dq_name = _val(dq, 'name', default="none")
    if dq_id in dict_s['DQSystem']['ids']:
        idx = dict_s['DQSystem']['ids'].index(dq_id)
        dq_obj = dict_s['DQSystem']['objs'][idx]
        logging.debug("Found existing DQSystem, %s" % dq_obj.name)
    else:
        logging.debug("Creating new DQSystem entity for '%s'" % dq_name)
        dq_obj = o.DQSystem()
        # HOTFIX: pre-defined UUIDs are set using version 4 (not 3)
        if not _uid_is_valid(dq_id, 4):
            # HOTFIX: add standard UUID naming
            logging.debug("Generating DQSystem UUID")
            dq_id = _uid(o.ModelType.DQ_SYSTEM, dq_type, dq_name)
        dq_obj.id = dq_id
        dq_obj.name = dq_name
        dq_obj.description = dq_desc
        # NOTE: uncertainty, indicators, and source are not included here!
        dict_s['DQSystem']['ids'].append(dq_obj.id)
        dict_s['DQSystem']['objs'].append(dq_obj)
    return (dq_obj.to_ref(), dict_s)


def _exchange(dict_d, dict_s):
    """Generate an Exchange object.

    Note that the process_dictionary_writer.py methods responsible for
    generating the data dictionary for exchanges does not include location
    and includes 'baseUncertainty' and 'pedigreeUncertainty', which
    appear to be data quality indicators.

    Note also that the internalId is meant to be an integer and helps identify
    duplicate input or output flows within the same process, so they can be
    linked to different providers.

    Parameters
    ----------
    dict_d : dict
        Data dictionary for an exchange.
        Expected keys are defined in `exchange_table_creation_*` methods
        found in process_dictionary_writer.py, and may include the following:

        - internalID : str (meant to be unique integer within a process)
        - avoidedProduct : bool
        - comment : str
        - flow : dict
        - flowProperty: str
        - input: bool
        - quantitativeReference : bool
        - baseUncertainty : str
        - provider : str
        - amount : float
        - amountFormula : str
        - unit : dict
        - pedigreeUncertainty : str

    dict_s : dict
        Dictionary with olca-schema root entity information.

    Returns
    -------
    tuple
        olca_schema.Exchange : Exchange object (or NoneType)
        dict : The olca-schema root entity dictionary, updated
    """
    # Error handle missing data:
    if dict_d is None:
        logging.debug("No exchange data!")
        return (None, dict_s)

    e = o.Exchange.from_dict({
        'isQuantitativeReference': _val(
            dict_d, 'quantitativeReference', default=False),
        'isInput': _val(dict_d, 'input', default=False),
        'isAvoidedProduct': _val(dict_d, 'avoidedProduct', default=False),
        'amount': _val(dict_d, 'amount', default=0.0),
        'dqEntry': _format_dq_entry(_val(dict_d, 'dqEntry')),
        'description': _val(dict_d, 'comment')
    })

    # Set unit (uses olca unit references)
    unit_name = _val(dict_d, 'unit', default='kg')
    e.unit = _unit(unit_name)

    # Set reference to flow property
    f_prop = _flow_property(unit_name, dict_s)
    if f_prop is not None:
        e.flow_property = f_prop.to_ref()

    # Set flow and uncertainty
    e.flow, dict_s = _flow(_val(dict_d, 'flow'), f_prop, dict_s)
    e.uncertainty = _uncertainty(_val(dict_d, 'uncertainty'))

    # Find the provider process reference (or create one);
    #  note that this does not update the dict_s entries, but searches them!
    #  BUG: are you sure this doesn't update dict_s?
    p_ref, dict_s, _ = _process(_val(dict_d, 'provider'), dict_s)
    if p_ref is not None:
        e.default_provider = p_ref.to_ref()

    return (e, dict_s)


def _exchange_list(dict_d, dict_s):
    """Generates a list of exchanges.

    Parameters
    ----------
    dict_d : dict
        Data dictionary for a process.
        Expected key is 'exchanges', which should return a list of
        dictionaries, where each dictionary describes an exchange.
    dict_s : dict
        Data dictionary for storing olca-schema root entities.

    Returns
    -------
    tuple
        list : List of Exchange objects
        dict : The olca-schema root entity dictionary, updated
        olca_schema.Exchange or NoneType : quantitative reference exchange
    """
    r_list = []
    last_id = 0
    q_ref = None
    for e in _val(dict_d, 'exchanges', default=[]):
        ex_obj, dict_s = _exchange(e, dict_s)
        if ex_obj is not None:
            last_id += 1
            ex_obj.internal_id = last_id
            r_list.append(ex_obj)

            # NOTE: there should be at most one quantitative reference in an
            # exchange list; if there is more than one, then the last
            # instance is returned.
            if ex_obj.is_quantitative_reference:
                q_ref = ex_obj
    return (r_list, dict_s, q_ref)


def _find_dq(dict_d, dict_key):
    """Search a process dictionary (and its documentation) for a given data
    quality attribute.

    Parameters
    ----------
    dict_d : dict
        Process (or processDocumentation) dictionary.
    dict_key : str
        Data quality key (e.g., 'dqEntry', 'dqSystem' or 'exchageDqSystem')

    Returns
    -------
    str, dict
        Data quality value.
    """
    dq = _val(dict_d, dict_key)
    if isinstance(dict_d, dict) and not dq:
        # NOTE: dq attributes may be found under processDocumentation!
        logging.debug("Searching process documentation for data quality key!")
        dq = _find_dq(_val(dict_d, 'processDocumentation'), dict_key)
    return dq


def _find_ref_exchange(p):
    """Return the exchange class object associated as the quantitative
    reference.

    Note that there should be only one in a list of exchanges for a single
    process.

    Parameters
    ----------
    p : o.Process
        An olca_schema.Process class.

    Returns
    -------
    o.Exchange
        An olca_schema.Exchange object (or NoneType if not found)
    """
    e_obj = None
    if p.exchanges is None or len(p.exchanges) == 0:
        pass
    else:
        for e in p.exchanges:
            if e.is_quantitative_reference:
                e_obj = e
    return e_obj


def _flow(dict_d, flowprop, dict_s):
    """Generate a reference to a flow object.

    Called by :func:`_exchange`.

    Parameters
    ----------
    dict_d : dict
        Flow data dictionary.
    flowprop : olca_schema.FlowProperty or olca_schema.Ref
        FlowProperty or Ref to a FlowProperty object.
    dict_s : dict
        Dictionary with olca_schema root entities.

    Returns
    -------
    tuple
        olca_schema.Ref : Reference to a flow object.
        dict : Updated dictionary with olca_schema root entities.
    """
    if not isinstance(dict_d, dict):
        logging.warning("No flow data received!")
        return (None, dict_s)

    uid = _val(dict_d, 'id', '@id')
    name = _val(dict_d, 'name')
    category_path = _val(dict_d, 'category', default='')
    is_waste = "waste" in category_path.lower()

    # HOTFIX: remove technosphere/3rd party flow check;
    # it duplicates every waste flow in the JSON-LD [2023-12-05; TWD]

    # Check for flow existence
    if uid in dict_s['Flow']['ids']:
        idx = dict_s['Flow']['ids'].index(uid)
        flow = dict_s['Flow']['objs'][idx]
        logging.debug("Found previous flow, '%s'" % flow.name)
    else:
        logging.debug("Creating new flow for, '%s' (%s)" % (name, uid))
        if _uid_is_valid(uid, 3) or _uid_is_valid(uid, 4):
            # Keep the good UUID
            pass
        else:
            # Generate new v3 ID based on standard format
            logging.debug("Generating new UUID for flow, '%s'" % name)
            uid = _uid(o.ModelType.FLOW, category_path, name)

        # Correct the default flow type for waste flows.
        def_type = "ELEMENTARY_FLOW"
        if is_waste:
            dict_d['flowType'] = "WASTE_FLOW"
            def_type = "WASTE_FLOW"

        f_type = _flow_type(_val(dict_d, 'flowType', default=def_type))
        flow = o.new_flow(name, f_type, flowprop)

        # Add perfunctory flow metadata now; update w/ FEDEFL metadata in post
        flow.id = uid
        flow.category = category_path

        # Update master list
        dict_s['Flow']['ids'].append(uid)
        dict_s['Flow']['objs'].append(flow)
    return (flow.to_ref(), dict_s)


def _flow_property(unit_name, dict_s):
    """Return the openLCA flow property reference for the given unit.

    Parameters
    ----------
    unit_name : str or dict
        The unit name (e.g., 'kg') or unit dictionary as generated by
        the unit() method in process_dictionary_writer.py.
    dict_s : dict
        The dictionary with openLCA root entity data.

    Returns
    -------
    olca_schema.FlowProperty
        Flow property object (or NoneType, if not defined).

    Examples
    --------
    >>> import pandas as pd
    >>> f = ("https://github.com/GreenDelta/olca-schema/"
    ...      "blob/master/py/olca_schema/units/units.csv")
    >>> df = pd.read_csv(f)
    >>> list(df['flow property name'].unique())
    ['Area*time',
     'Duration',
     'Market value, bulk prices',
     'Volume',
     'Person transport',
     'Goods transport (mass*distance)',
     'Area',
     'Radioactivity',
     'Mass*time',
     'Length*time',
     'Length',
     'Volume*Length',
     'Mass',
     'Energy',
     'Vehicle transport',
     'Biotic Production (Transf.)',
     'Volume*time',
     'Number of items',
     'Energy/area*time',
     'Mechanical Filtration (Transf.)',
     'Items*Length',
     'Groundwater Replenishment (Transf.)',
     'Energy/mass*time',
     'Groundwater Replenishment (Occ.)',
     'Physicochemical Filtration (Transf.)',
     'Mechanical Filtration (Occ.)',
     'Physicochemical Filtration (Occ.)']
    """
    r_obj = None
    if isinstance(unit_name, dict):
        try:
            unit_name = unit_name["name"]
        except KeyError:
            logging.error(
                "Missing the required 'name' key in unit dictionary!")
            return r_obj

    # HOTFIX: reference new flow property list
    p_ref = o_units.property_ref(unit_name)
    if p_ref is None:
        logging.error(
            "Unknown unit, '%s'; no flow property reference!" % unit_name)
    elif p_ref.id in dict_s['FlowProperty']['ids']:
        logging.debug("Reading existing flow property")
        pid = dict_s['FlowProperty']['ids'].index(p_ref.id)
        r_obj = dict_s['FlowProperty']['objs'][pid]
    else:
        # Assumes federal elementary flow list was used to populate flow
        # properties; therefore, the old way of trying to recreate a
        # flow property from only Ref objects is removed.
        logging.info("Failed to find flow property for '%s'" % unit_name)

    return r_obj


def _flow_type(f_type):
    """Retrieve olca-schema FlowType object.

    Parameters
    ----------
    f_type : str
        Flow type (e.g., "ELEMENTARY_FLOW", "PRODUCT_FLOW" or "WASTE_FLOW").

    Returns
    -------
    olca_schema.FlowType
        An enum type as defined in olca-schema package.
    """
    for i in o.FlowType:
        if i.value == f_type:
            return i
    return None


def _format_date(entry):
    """Convert traditional M/D/YYYY date string to ISO format.

    Parameters
    ----------
    entry : str
        Date string in the format: month/day/year.

    Returns
    -------
    str
        ISO-formatted date string, 'YYYY-MM-DDZHH:MM:SS'.
         returns NoneType.
    """
    try:
        d_obj = datetime.datetime.strptime(entry, '%m/%d/%Y')
    except TypeError:
        logging.warning("Expected date as string, found %s" % type(entry))
        return None
    except ValueError:
        logging.warning(
            "Received unexpected date format (M/D/YYYY): '%s'" % entry)
        return None
    except Exception as e:
        logging.warning("Encountered an unexpected error. %s" % str(e))
        return None
    else:
        return d_obj.isoformat()


def _format_dq_entry(entry):
    """Format data quality entries.

    Parameters
    ----------
    entry : str
        Data quality entry, like (1;2;5;3).

    Returns
    -------
    str
        Data quality entry where floating point numbers are converted to ints.
        Returns NoneType if an invalid parameter is encountered.

    Notes
    -----
    For more information on data quality strings, see
    https://greendelta.github.io/olca-schema/classes/Process.html#dqentry
    """
    if not isinstance(entry, str):
        return None
    e = entry.strip()
    if len(e) < 2:
        return None
    e = e.rstrip(')').lstrip('(')
    nums = e.split(';')
    for i in range(len(nums)):
        if ((nums[i] == 'n.a.') or (nums[i] == "nan")):
            continue
        else:
            nums[i] = str(round(float(nums[i])))
    return '(%s)' % ';'.join(nums)


def _heat_elem_flow():
    """Returns Energy, heat resource from FEDEFL Elementary Flow List

    Returns
    -------
    o.Flow
        Flow object for 'Energy, heat' as found in the Commons' FEDEFL.

    Notes
    -----
    The dictionary used to create this flow object is based on the downloaded
    JSON-LD found `here <https://www.lcacommons.gov/lca-collaboration/Federal_LCA_Commons/elementary_flow_list/dataset/FLOW/8c959db8-d359-36e3-8517-588e1c21df4a>`_.
    """
    json_dict = {
        "@type":"Flow",
        "@id":"8c959db8-d359-36e3-8517-588e1c21df4a",
        "name":"Energy, heat",
        "description":"From Federal Elementary Flow List v1.3.0, written by fedelemflowlist v1.3.0. Flow Class: Energy. Preferred flow.",
        "category":"Elementary flows/resource/air",
        "version":"01.03.000",
        "lastChange":"2024-12-28T13:02:35.764Z",
        "flowType":"ELEMENTARY_FLOW",
        "isInfrastructureFlow":False,
        "flowProperties":[{
            "@type":"FlowPropertyFactor",
            "isRefFlowProperty":True,
            "flowProperty":{
                "@type":"FlowProperty",
                "@id":"f6811440-ee37-11de-8a39-0800200c9a66",
                "name":"Energy",
                "category":"Technical flow properties",
                "refUnit":"MJ"
            },
            "conversionFactor":1.0
        }]
    }
    heat_flow = o.Flow.from_dict(json_dict)

    return heat_flow


def _init_root_entities(json_file):
    """Generate dictionary for each openLCA schema root entity.

    Check for JSON-LD file existence and read root entities from file;
    otherwise, pre-populate with GreenDelta's UnitGroups and FlowProperties.

    Returns
    -------
    dict
        Dictionary with primary keys for each root entity (camel-case).
        The values are dictionaries with three keys: 'class', 'objs', and 'ids'.
        The 'ids' list is for quick referencing and 'objs' list is for actual
        writing to file. The 'class' is value added (if needed).
    """
    # Create the empty dictionary for each olca schema root entity
    # (these are the ones that need to be written to the JSON-LD zip file)
    r_dict = _root_entity_dict()

    # Check to see if the JSON-LD file was already written to;
    # if so, read the old root entity data; otherwise, add data from the
    # Federal LCA Commons (e.g., unit groups, flow properties, and DQI data).
    if os.path.exists(json_file):
        r_dict = _read_jsonld(json_file, r_dict)
    else:
        r_dict = _add_fed_commons(r_dict)

    return r_dict


def _isnum(n):
    """Type checking for number.

    Performs no type casting of number strings (e.g., '23' is not a number).

    Parameters
    ----------
    n : Any
        A Python object (e.g., str, int, float) to be tested as numeric.

    Returns
    -------
    bool
        Whether the argument is a number.
    """
    if not isinstance(n, (float, int)):
        return False
    return not math.isnan(n)


def _location(dict_d, dict_s):
    """Create a new location or reference an existing one.

    Notes
    -----
    Overwrites openLCA's random UUID generator.

    Parameters
    ----------
    dict_d : dict or str
        A dictionary with location data or a string of location name.
    dict_s : dict
        Dictionary with created root entities.

    Returns
    -------
    tuple
        olca.Ref :
            A reference object to an olca Location.
        dict :
            The dict_s, updated.

    Notes
    -----
    For information on Location attributes, see
    https://greendelta.github.io/olca-schema/classes/Location.html
    """
    # Check for missing location code (e.g., ISO 2-letter country code)
    # No code, no location!
    # HOTFIX: locations may just be a string [2023-11-14; TWD]
    if isinstance(dict_d, str):
        code = dict_d
        uid = None
    elif isinstance(dict_d, dict):
        code = _val(dict_d, 'name')
        uid = _val(dict_d, 'id', '@id')
    else:
        code = ""
        uid = ""

    if not isinstance(code, str):
        return (None, dict_s)
    if code == '':
        return (None, dict_s)

    # Check for valid UUID; otherwise, generate one
    if not _uid_is_valid(uid):
        uid = _uid(o.ModelType.LOCATION, code)

    # Check if location already exists in our records; otherwise, create
    # and record the new location.
    if uid in dict_s['Location']['ids']:
        idx = dict_s['Location']['ids'].index(uid)
        location = dict_s['Location']['objs'][idx]
        logging.debug("Using existing location, %s" % location.name)
    else:
        logging.debug("Creating new location entry for '%s'" % code)
        location = o.Location(id=uid, code=code)
        location.name = code
        location.latitude = _val(dict_d, 'latitude')
        location.longitude = _val(dict_d, 'longitude')
        location.description = _val(dict_d, 'description')
        dict_s['Location']['ids'].append(uid)
        dict_s['Location']['objs'].append(location)
    return (location.to_ref(), dict_s)


def _make_entity_dict(e_dict, e_key):
    """Convenience function to convert two lists into a single dictionary.

    Parameters
    ----------
    e_dict : dict
        A data dictionary containing olca schema UUIDs (ids) and their respective class objects (objs)
    e_key : str
        The data dictionary key to convert into a dictionary; corresponds to olca root entity names (e.g., 'Actor' or 'Flow')

    Returns
    -------
    dict
        A dictionary of UUID keys and their class objects as values.
    """
    r_dict = {}
    num_ids = len(e_dict[e_key]['ids'])
    for i in range(num_ids):
        uid = e_dict[e_key]['ids'][i]
        try:
            obj = e_dict[e_key]['objs'][i]
        except IndexError:
            # Happens, for example, if current data dictionary was
            # created with IDs only (i.e., no objects). Since we
            # don't have the object data, it isn't getting copied!
            logging.warning("Skipping %s (%s)! Missing class info!" % (
                e_key, uid))
        else:
            r_dict[uid] = obj

    return r_dict


def _make_product_system(f_path, process, description=""):
    """Generate a product system for a given process.

    Parameters
    ----------
    f_path : str
        A file path to an existing JSON-LD file with all process data saved.
    process : olca-schema.Process
        A Process object to be converted to a Product System.
    description : str, optional
        The product system description text, by default ""

    Returns
    -------
    olca-schema.ProductSystem
        A product system build on default providers for the given process.
    """
    # Find the reference process
    r_ex = _find_ref_exchange(process)

    # Create a new product system
    # https://greendelta.github.io/olca-schema/classes/ProductSystem.html
    # NOTE: ``last_change`` automatically set to current date/time
    product = o.ProductSystem(
        description=description,
        name=process.name,
        ref_exchange=o.ExchangeRef(r_ex.internal_id),
        ref_process=process.to_ref(),
        target_amount=r_ex.amount,
        target_flow_property=r_ex.flow_property,
        target_unit=r_ex.unit,
        version=process.version
    )

    f = zipio.ZipReader(f_path)

    # Build processLinks and processes; hotfix w/ empty lists
    ex_list, pd_list = _build_supply_chain(f, process.id, [], [])
    product.processes = [
        _make_process_ref(f.read(o.Process, x)) for x in pd_list]
    product.process_links = ex_list

    f.close()

    return product


def _make_process_ref(p_obj):
    """Generate a Ref object for a given process, preserving as much
    metadata as possible.

    Parameters
    ----------
    p_obj : olca_schema.Process
        A process object.

    Returns
    -------
    olca_schema.Ref
        A reference object to the given process.

    Notes
    -----
    An attempt to make up for poor `to_ref` class method.
    See https://github.com/GreenDelta/olca-schema/issues/8
    """
    ref_obj = o.Ref()
    if isinstance(p_obj, o.Process):
        ref_obj = o.Ref(
            id=p_obj.id,
            category=p_obj.category,
            description=p_obj.description,
            location=p_obj.to_dict().get("location", {}).get("name", ""),
            process_type=p_obj.process_type,
            ref_type=o.RefType.Process,
        )
    return ref_obj


def _match_process_names(p_list, q):
    """Return a list of process UUIDs that match a given name query.

    Parameters
    ----------
    p_list : list
        A list of Process objects (e.g., as read from JSON-LD file).
    q : re.Pattern
        A regular expression pattern object.
        For example, ``q = re.compile("^Electricity; at grid; .*")``

    Returns
    -------
    list
        A list of process universally unique identifiers (str)

    Notes
    -----
    This should also work on other root entities with a name and id
    attribute (e.g., ProductSystem).
    """
    r_list = []
    for ref in p_list:
        r = q.match(ref.name)
        if r:
            r_list.append(ref.id)
    return r_list


def _process(dict_d, dict_s):
    """Generate a new Process object.

    If the process includes exchanges, and one of the exchanges is marked
    as a quantitative reference, then that exchange object is also returned.

    Parameters
    ----------
    dict_d : dict
        Process data dictionary.
    dict_s : dict
        olca-schema root entity dictionary.

    Returns
    -------
    tuple
        olca_schema.Process or NoneType : the process object
        dict : the root entity dictionary, updated
        olca_schema.Exchange or NoneType : quantitative reference exchange
    """
    if not isinstance(dict_d, dict):
        return (None, dict_s, None)

    uid = _val(dict_d, '@id')
    name = _val(dict_d, 'name')
    category = _val(dict_d, 'category', default='')
    location_code = _val(dict_d, 'location', 'name', default='')

    # Generate the standardized UUID, if absent
    if uid is None:
        logging.debug("Generating new process UUID for '%s'" % name)
        uid = _uid(
            o.ModelType.PROCESS,
            category,
            location_code,
            name)

    # Check for process existence:
    if uid in dict_s['Process']['ids']:
        idx = dict_s['Process']['ids'].index(uid)
        p = dict_s['Process']['objs'][idx]
        logging.debug("Found existing process, %s" % p.name)
        # HOTFIX: add missing e_ref from existing process
        e_ref = _find_ref_exchange(p)
    else:
        logging.debug("Creating new Process entity for '%s'" % name)
        p = o.new_process(name=name)
        p.id = uid
        p.category = category
        p.version = _val(dict_d, 'version', default=VERSION)
        p.description = _val(dict_d, 'description')

        # The olca_schema.new_process() defaults to UNIT PROCESS.
        # Check for other type (i.e., LCI result)
        p_type = _val(dict_d, 'processType', default='UNIT_PROCESS')
        if p_type != "UNIT_PROCESS":
            logging.debug("Setting process type to LCI RESULT")
            p.process_type = o.ProcessType.LCI_RESULT

        # No location will return a none-type.
        p.location, dict_s = _location(_val(dict_d, 'location'), dict_s)
        p.process_documentation, dict_s = _process_doc(
            _val(dict_d, 'processDocumentation'),
            dict_s
        )

        # DQSystems have pre-defined UUIDs (v.4);
        #   see process_dictionary_writer.py
        p.dq_entry = _dq_entry(dict_d)
        p.dq_system, dict_s = _dq_system(dict_d, dict_s, 'dqSystem')
        p.exchange_dq_system, dict_s = _dq_system(
            dict_d, dict_s, 'exchangeDqSystem'
        )

        p.exchanges, dict_s, e_ref = _exchange_list(dict_d, dict_s)

    return (p, dict_s, e_ref)


def _process_doc(dict_d, dict_s):
    """Generate process documentation for an olca-schema Process object.

    Parameters
    ----------
    dict_d : dict
        A dictionary with process documentation.
    dict_s : dict
        Dictionary with olca-schema root entities.

    Returns
    -------
    tuple
        olca_schema.ProcessDocumentation : Process documentation object.
        dict : Updated dictionary with olca-schema root entities, ``dict_s``.

    Notes
    -----
    For details on the expected properties for process documentation, see:
    https://greendelta.github.io/olca-schema/classes/ProcessDocumentation.html
    """
    # Copy the fields that have the same format as in the olca-schema spec.
    copy_fields = [
        'timeDescription',
        'technologyDescription',
        'dataCollectionDescription',
        'completenessDescription',
        'dataSelectionDescription',
        'reviewDetails',
        'dataTreatmentDescription',
        'inventoryMethodDescription',
        'modelingConstantsDescription',
        'samplingDescription',
        'restrictionsDescription',
        'copyright',
        'intendedApplication',
        'projectDescription',
    ]

    doc = o.ProcessDocumentation()

    # Check to see if process documentation was sent; if so, update
    if isinstance(dict_d, dict):
        doc = doc.from_dict(
            {field: _val(dict_d, field) for field in copy_fields}
        )
        doc.valid_from = _format_date(_val(dict_d, 'validFrom'))
        doc.valid_until = _format_date(_val(dict_d, 'validUntil'))

        # Add Actor references
        doc.reviewer, dict_s = _actor(
            _val(dict_d, 'reviewer'),
            dict_s
        )
        doc.data_documentor, dict_s = _actor(
            _val(dict_d, 'dataDocumentor'),
            dict_s
        )
        doc.data_generator, dict_s = _actor(
            _val(dict_d, 'dataGenerator'),
            dict_s
        )
        doc.data_set_owner, dict_s = _actor(
            _val(dict_d, 'dataSetOwner'),
            dict_s
        )

        # Update sources
        doc.publication, dict_s = _source(_val(dict_d, 'publication'), dict_s)
        doc.sources, dict_s = _source_list(
            _val(dict_d, 'sources', default=[]),
            dict_s
        )
    doc.creation_date = _current_time()
    return (doc, dict_s)


def _read_fedefl():
    """Return list of GreenDelta's unit group and flow property objects.

    This utilizes the Federal LCA Commons' public API to pull data from the
    Elementary Flow List repository.

    A local copy of the LCA Commons' Federal Elementary Flow List unit groups
    is either accessed (in eLCI's data directory) or created (using requests).

    Notes
    -----
    This method writes up to two files in electricitylci's data directory:

    -   flow_properties.json
    -   unit_groups.json

    Returns
    -------
    tuple
        A tuple of length two.
        First item is a list of 27 olca-schema UnitGroup objects.
        Second item is a list of 33 olca-schema FlowProperty objects.
    """
    # Define the base URL for the public API
    url = (
        "https://www.lcacommons.gov/"
        "lca-collaboration/ws/public/download/json"
    )

    # Using "path=FLOW_PROPERTY" obtains all Flow properties and unit groups
    token_url = url + (
        "/prepare/Federal_LCA_Commons/elementary_flow_list?path=FLOW_PROPERTY"
    )

    # NEW data store [25.02.12; TWD]
    data_dir = os.path.join(paths.local_path, "fedcommons")
    check_output_dir(data_dir)

    u_file = "unit_groups.json"
    u_path = os.path.join(data_dir, u_file)
    u_list = []

    p_file = "flow_properties.json"
    p_path = os.path.join(data_dir, p_file)
    p_list = []

    if not os.path.exists(u_path) or not os.path.exists(p_path):
        # Pull from Federal Elementary Flow List
        logging.info("Reading data from Federal LCA Commons")
        #adding 20s timeout to avoid long delays due to server issues.
        token = requests.get(token_url, timeout=20).content.decode()
        r = requests.get(f"{url}/{token}", timeout=20)
        if r.ok:
            with ZipFile(io.BytesIO(r.content)) as z:
                # Find the unit groups, convert them to UnitGroup class
                for name in z.namelist():
                    # Note there are only three folders in the zip file:
                    # 'flow_properties', 'flows', and 'unit_groups';
                    # we want the 27 JSON files under unit_groups
                    # and the 33 JSON files under flow_properties.
                    if name.startswith("unit") and name.endswith("json"):
                        u_dict = json.loads(z.read(name))
                        u_obj = o.UnitGroup.from_dict(u_dict)
                        u_list.append(u_obj)
                    elif name.startswith("flow_") and name.endswith("json"):
                        p_dict = json.loads(z.read(name))
                        p_obj = o.FlowProperty.from_dict(p_dict)
                        p_list.append(p_obj)
        else:
            logging.error(
                "Failed to access Elementary Flow List on Fed Commons!")

        # Archive to avoid running requests again.
        _archive_json(u_list, u_path)
        logging.info("Saved unit groups from LCA Commons to JSON")

        _archive_json(p_list, p_path)
        logging.info("Saved flow properties from LCA Commons to JSON")

    # Only read locally if needed (i.e., if data wasn't just downloaded)
    if os.path.exists(u_path) and len(u_list) == 0:
        logging.info("Reading unit groups from local JSON")
        with open(u_path, 'r') as f:
            my_list = json.load(f)
        for my_item in my_list:
            u_list.append(o.UnitGroup.from_dict(my_item))

    if os.path.exists(p_path) and len(p_list) == 0:
        logging.info("Reading flow properties from local JSON")
        with open(p_path, 'r') as f:
            my_list = json.load(f)
        for my_item in my_list:
            p_list.append(o.FlowProperty.from_dict(my_item))

    return (u_list, p_list)


def _read_fedcore():
    """Return list of GreenDelta's DQSystem and Source objects.

    This utilizes the Federal LCA Commons' public API to pull data from the
    core database repository.

    A local copy of the LCA Commons' DQSystems and Source objects
    is either accessed (in user's data directory) or created (using requests).

    Notes
    -----
    This method writes up to two files in electricitylci's data store:

    -   dq_systems.json
    -   dq_sources.json

    Returns
    -------
    tuple
        A tuple of length two.
        First item is a list of 2 olca-schema DQSystem objects.
        Second item is a list of 1 olca-schema Source objects.
    """
    # Define the base URL for the public API
    url = (
        "https://www.lcacommons.gov/"
        "lca-collaboration/ws/public/download/json"
    )

    # Token for JSON-LD with data quality indicators
    token_url = url + (
        "/prepare/Federal_LCA_Commons/Fed_Commons_core_database?path=DQ_SYSTEM"
    )

    # Save data to the user's electricitylci data store.
    data_dir = os.path.join(paths.local_path, "fedcommons")
    check_output_dir(data_dir)

    d_file = "dq_systems.json"
    d_path = os.path.join(data_dir, d_file)
    d_list = []

    s_file = "dq_sources.json"
    s_path = os.path.join(data_dir, s_file)
    s_list = []

    if not os.path.exists(d_path) or not os.path.exists(s_path):
        # Pull from Federal Elementary Flow List
        logging.info("Reading data from Federal LCA Commons")
        #adding 20s timeout to avoid long delays due to server issues.
        token = requests.get(token_url, timeout=20).content.decode()
        r = requests.get(f"{url}/{token}", timeout=20)
        if r.ok:
            with ZipFile(io.BytesIO(r.content)) as z:
                for name in z.namelist():
                    # Note there are only two folders in the zip file:
                    # 'dq_systems' and 'sources'
                    if name.startswith("dq_systems") and name.endswith("json"):
                        d_dict = json.loads(z.read(name))
                        d_obj = o.DQSystem.from_dict(d_dict)
                        d_list.append(d_obj)
                    elif name.startswith("sources") and name.endswith("json"):
                        s_dict = json.loads(z.read(name))
                        s_obj = o.Source.from_dict(s_dict)
                        s_list.append(s_obj)
        else:
            logging.error(
                "Failed to access Elementary Flow List on Fed Commons!")

        # Archive to avoid running requests again.
        _archive_json(d_list, d_path)
        logging.info("Saved DQSystems from LCA Commons to JSON")

        _archive_json(s_list, s_path)
        logging.info("Saved DQI sources from LCA Commons to JSON")

    # Only read locally if needed (i.e., if data wasn't just downloaded)
    if os.path.exists(d_path) and len(d_list) == 0:
        logging.info("Reading DQSystems from local JSON")
        with open(d_path, 'r') as f:
            my_list = json.load(f)
        for my_item in my_list:
            d_list.append(o.DQSystem.from_dict(my_item))

    if os.path.exists(s_path) and len(s_list) == 0:
        logging.info("Reading DQI sources from local JSON")
        with open(s_path, 'r') as f:
            my_list = json.load(f)
        for my_item in my_list:
            s_list.append(o.Source.from_dict(my_item))

    return (d_list, s_list)


def _read_jsonld(json_file, root_dict, id_only=False):
    """Read root entities from JSON-LD file and append to root entity
    dictionary.

    Parameters
    ----------
    json_file : str
        A file path to a zipped JSON-LD file.
    root_dict : dict
        A dictionary with olca-schema root entity data (as provided by
        :func:`_root_entity_dict`).
    id_only : bool
        Whether to save UUIDs and olca-schema class objects.
        If true, only 'ids' list is read.

    Returns
    -------
    dict
        The same root entity dictionary with 'ids' and 'objs' lists updated.

    Raises
    ------
    OSError
        If the JSON-LD file path does not exist (or is not a file).

    Notes
    -----
    -   Methods are based on those from NETL's NetlOlca Python class.
    -   Reads full Class objects into memory (when `id_only` is false),
        which may be large for large projects (e.g., >2000 flows in the
        2016 baseline).

    Examples
    --------
    >>> my_file = "Federal_LCA_Commons-US_electricity_baseline.zip" # 2016 b.l.
    >>> my_dict = _read_jsonld(my_file, _root_entity_dict(), False)
    >>> print(
    ...     'Process',
    ...     len(my_dict['Process']['ids']), 'UUIDs',
    ...     len(my_dict['Process']['objs']), 'objects')
    Process 606 UUIDs 606 objects
    >>> my_dict = _read_jsonld(my_file, _root_entity_dict(), True)
    >>> print(
    ...     'Process',
    ...     len(my_dict['Process']['ids']), 'UUIDs',
    ...     len(my_dict['Process']['objs']), 'objects')
    Process 606 UUIDs 0 objects
    """
    if not os.path.isfile(json_file):
        raise OSError("File not found! %s" % json_file)
    else:
        # Create a file handle to the JSON-LD zip
        logging.info("Opening JSON-LD file, %s" % os.path.basename(json_file))
        j_file = zipio.ZipReader(json_file)
        for name in root_dict.keys():
            # Get IDs for each root entity
            spec = root_dict[name]['class']
            r_ids = j_file.ids_of(spec)
            logging.info("Read %d UUIDs for %s" % (len(r_ids), name))
            # Get the root entity object based on its type
            for rid in r_ids:
                # Only read from file when a new UUID is found.
                # (easier to debug this way, rather than a set for unique vals)
                if rid in root_dict[name]['ids']:
                    logging.debug(
                        "Skipping existing UUID for %s (%s)" % (name, rid))
                else:
                    if id_only:
                        root_dict[name]['ids'].append(rid)
                    else:
                        r_obj = None
                        try:
                            r_obj = j_file.read(spec, rid)
                        except Exception as e:
                            logging.warning(
                                "Failed to read %s (%s) from file! %s" % (
                                    name, rid, str(e)))
                        # Add the UUID and Class object pair to their lists
                        if r_obj is not None:
                            root_dict[name]['ids'].append(rid)
                            root_dict[name]['objs'].append(r_obj)
        j_file.close()

        return root_dict


def _rm_untracked_flows(data):
    """Post-processing method that identifies and removes untracked flows
    (i.e., flow objects that are not referenced in any exchanges).

    This is purely to reduce the size of the database.

    Parameters
    ----------
    data : dict
        A dictionary containing the olca-schema root element lists.

    Returns
    -------
    dict
        The same dictionary received, but with untracked flows removed
        from the Flow root element lists.

    Notes
    -----
    This finds 'Heat' technosphere input flow and elementary resource flow,
    which (somewhere in v2) are replaced with 'Energy, heat' elementary resource flow (from air).
    """
    # Pull full flow list (for removing untracked flows)
    f_list = sorted(data["Flow"]['ids'])

    # Initialize exchange flows
    e_list = []

    # Add flows to list of tracked exchanges
    for p in data["Process"]['objs']:
        for e in p.exchanges:
            e_list.append(e.flow.id)

    # Sort unique values to speed up search
    e_list = sorted(list(set(e_list)))

    # Remove untracked flows (i.e., any flows that aren't in an exchange)
    u_list = [x for x in f_list if x not in e_list]
    logging.info("Removing %d untracked flows" % len(u_list))
    for u_id in u_list:
        idx = data["Flow"]['ids'].index(u_id)
        logging.info("Untracked flow: '%s' in '%s'" % (
            data['Flow']['objs'][idx].name,
            data['Flow']['objs'][idx].category,
        ))
        data['Flow']['ids'].pop(idx)
        data['Flow']['objs'].pop(idx)

    return data


def _root_entity_dict():
    """Generate empty dictionary for each openLCA schema root entity.

    Returns
    -------
    dict
        Dictionary with primary keys for each root entity (camel-case).
        The values are dictionaries with three keys: 'class', 'objs', and 'ids'.
        The 'ids' list is for quick referencing and 'objs' list is for actual
        writing to file. The 'class' is value added (if needed).
    """
    return {
        'Actor': {
            'class': o.Actor,
            'objs': [],
            'ids': []},
        "Currency": {
            'class': o.Currency,
            'objs': [],
            'ids': []},
        'DQSystem': {
            'class': o.DQSystem,
            'objs': [],
            'ids': []},
        'EPD': {
            'class': o.Epd,
            'objs': [],
            'ids': []},
        'Flow': {
            'class': o.Flow,
            'objs': [],
            'ids': []},
        'FlowProperty': {
            'class': o.FlowProperty,
            'objs': [],
            'ids': []},
        'ImpactCategory': {
            'class': o.ImpactCategory,
            'objs': [],
            'ids': []},
        'ImpactMethod': {
            'class': o.ImpactMethod,
            'objs': [],
            'ids': []},
        'Location': {
            'class': o.Location,
            'objs': [],
            'ids': []},
        'Parameter': {
            'class': o.Parameter,
            'objs': [],
            'ids': []},
        'Process': {
            'class': o.Process,
            'objs': [],
            'ids': []},
        'ProductSystem': {
            'class': o.ProductSystem,
            'objs': [],
            'ids': []},
        'Project': {
            'class': o.Project,
            'objs': [],
            'ids': []},
        'Result': {
            'class': o.Result,
            'objs': [],
            'ids': []},
        'SocialIndicator': {
            'class': o.SocialIndicator,
            'objs': [],
            'ids': []},
        'Source': {
            'class': o.Source,
            'objs': [],
            'ids': []},
        'UnitGroup': {
            'class': o.UnitGroup,
            'objs': [],
            'ids': []}
    }


def _save_to_json(json_file, e_dict):
    """Write an entity dictionary to JSON-LD format.

    Parameters
    ----------
    json_file : str
        A file path to an existing or desired JSON-LD zip file.
    e_dict : dict
        An olca-schema entity dictionary where keys are entity names
        (e.g., 'Actor' and 'Flow') and the values are dictionaries
        containing lists of olca-schema objects (objs) and their universally
        unique identifiers (ids).
    """
    logging.info("Looking for %s" % os.path.basename(json_file))
    try:
        # Grab UUIDs and class objs from existing JSON-LD
        logging.info("Found existing data in JSON-LD")
        c_data = _read_jsonld(json_file, _root_entity_dict())
    except OSError:
        logging.info("No existing JSON-LD found")
        c_data = _root_entity_dict()
    else:
        logging.info("Successfully read data from previous JSON-LD")
        logging.info("Removing old archive file")
        os.remove(json_file)
    finally:
        # Update current data (c_data) with new (e_dict).
        # If JSON-LD exists, then current data are those UUIDs and class
        # objects from the file; otherwise, the current data is empty.
        e_dict = _update_data(c_data, e_dict)
        # Remove untracked flows; primarily to reduce database size.
        e_dict = _rm_untracked_flows(e_dict)

    logging.info("Writing to %s" % os.path.basename(json_file))
    with zipio.ZipWriter(json_file) as writer:
        for k in e_dict.keys():
            logging.info("Writing %d %s" % (len(e_dict[k]['ids']), k))
            if k == "Flow":
                # FEDEFL flows are not added as objects, write them separately
                # using fedelmflowlist.write_jsonld() [20240911; BY]
                flowlist = fedelemflowlist.get_flows()
                flows = flowlist[flowlist['Flow UUID'].isin(e_dict[k]['ids'])]
                fedelemflowlist.write_jsonld(flows, path=None, zw=writer)

            for k_obj in e_dict[k]['objs']:
                # Last chance to fix Ref's and it's not perfect.
                if isinstance(k_obj, o.Ref):
                    logging.warning("Found Ref object in JSON-LD writer!")
                    logging.debug("%s Ref (%s)" % (k, k_obj.id))
                    k_type = k_obj.ref_type.value
                    k_dict = k_obj.to_dict()
                    k_obj = e_dict[k_type]['class'].from_dict(k_dict)

                if k == "Flow" and k_obj.id in flows['Flow UUID'].values:
                    # all FEDEFL flows written above
                    continue
                logging.debug("Writing %s entity (%s)" % (k, k_obj.id))
                writer.write(k_obj)


def _source(src_data, dict_s):
    """Generate a reference to a source object.

    Parameters
    ----------
    src_data : dict
        Source data dictionary.
    dict_s : dict
        Dictionary with created olca schema root entities.

    Returns
    -------
    tuple
        olca_schema.Ref : Reference object to Source (or NoneType).
        dict : Root entities dictionary, updated (``dict_s``).

    Notes
    -----
    For explanation of source object keys, see:
    https://greendelta.github.io/olca-schema/classes/Source.html
    """
    # If no source data retrieved, skip it!
    if not isinstance(src_data, dict):
        return (None, dict_s)
    if "Name" not in src_data.keys() or src_data['Name'] == '':
        return (None, dict_s)

    # Search for category and use it in conjunction with name for UUID
    # NOTE: categories are forward-slash separated strings, see:
    # https://greendelta.github.io/olca-schema/classes/RootEntity.html#category
    try:
        if isinstance(src_data['Category'], list):
            category = "/".join(src_data['Category'])
        elif isinstance(src_data["Category"], str):
            category = src_data["Category"]
        else:
            category = ''
    except KeyError:
        category = ''
    finally:
        uid = _uid(o.ModelType.SOURCE, category, src_data["Name"])

    # Check if source already exists.
    # If so, retrieve it; otherwise, create new source and record it!
    if uid in dict_s['Source']['ids']:
        idx = dict_s['Source']['ids'].index(uid)
        source = dict_s['Source']['objs'][idx]
        logging.debug("Found existing source, %s" % source.name)
    else:
        logging.debug("Creating new source entity for '%s'" % src_data['Name'])
        source = o.Source()
        source.id = uid
        source.category = category
        source.name = src_data["Name"]
        source.url = _val(src_data, "Url", default=None)
        source.version = _val(src_data, "Version", default=VERSION)
        source.text_reference = _val(
            src_data, "TextReference", default=src_data['Name'])
        source.year = _check_source_year(_val(src_data, "Year"))
        dict_s['Source']['ids'].append(uid)
        dict_s['Source']['objs'].append(source)

    return (source.to_ref(), dict_s)


def _source_list(s_data, dict_s):
    """Process all sources within a source list.

    Parameters
    ----------
    s_data : list
        A list of source dictionaries.
    dict_s : dict
        Dictionary of olca-schema root entities.

    Returns
    -------
    tuple
        list : List of source reference objects.
        dict : Updated dictionary of olca-schema entities, ``dict_s``.
    """
    r_list = []
    if not isinstance(s_data, list) or len(s_data) == 0:
        logging.warning("No source data provided!")
    else:
        for d in s_data:
            s_ref, dict_s = _source(d, dict_s)
            # skip missing references
            if s_ref:
                r_list.append(s_ref)
    return (r_list, dict_s)


def _uid(*args):
    """Generate UUID from the MD5 hash of a namespace identifier and a name.

    This method uses OID namespace, which assumes that the name is an ISO OID.
    Essentially, two strings are hashed together to create a UUID and, if the
    same namespace and path are given again, the same UUID would be returned.

    Warning
    -------
    The UUIDs generated by this method are version 3, which is different
    from standard processes defined elsewhere (e.g., DQSystems and Units).

    Parameters
    ----------
    args : tuple
        A tuple of key words representing a path (order matters).
        The path is a string with each argument separated by a forward slash.
        For flows, the path is 'modeltype.flow', flow name, compartment, and
        unit.
        For processes, the path is 'modeltype.process', process category,
        location, and name.

    Returns
    -------
    str
        A version 3 universally unique identifier (UUID)
    """
    path = '/'.join([str(arg).strip() for arg in args]).lower()
    logging.debug(path)
    return str(uuid.uuid3(uuid.NAMESPACE_OID, path))


def _uid_is_valid(uuid_str, version=3):
    """Check if string is a valid UUID.

    Parameters
    ----------
    uuid_str : str
    version : {1, 2, 3, 4}

    Returns
    -------
    bool
        `True` if uuid_str is a valid UUID, otherwise `False`.

    Examples
    --------
    >>> _uid_is_valid('c9bf9e57-1685-4c89-bafb-ff5af830be8a', 4)
    True
    >>> _uid_is_valid('c9bf9e58')
    False

    Notes
    -----
    Code snipped by Rafael (2020). CC-BY-SA 4.0. Online:
    https://stackoverflow.com/a/33245493
    """
    # HOTFIX: deal with non-strings (e.g., nan) [2023-11-14; TWD]
    try:
        uuid_obj = uuid.UUID(uuid_str, version=version)
    except (TypeError, ValueError, AttributeError):
        return False
    return str(uuid_obj) == uuid_str


def _uncertainty(dict_d):
    """Generate an uncertainty object.

    Creates an uncertainty object with a log-normal distribution with
    geometric mean and geometric standard deviation provided by the data
    dictionary.

    Parameters
    ----------
    dict_d : dict
        Uncertainty data dictionary.
        See uncertainty_table_creation in process_dictionary_writer.py.

    Returns
    -------
    olca_schema.Uncertainty
        A log-normal distribution uncertainty object.
        Returns NoneType for missing or invalid types.
    """
    if not isinstance(dict_d, dict):
        return None

    dt = _val(dict_d, 'distributionType')
    if dt != 'Logarithmic Normal Distribution':
        logging.debug("Found invalid uncertainty method, '%s'" % dt)
        return None

    gmean = _val(dict_d, 'geomMean')
    if isinstance(gmean, str):
        gmean = float(gmean)

    gsd = _val(dict_d, 'geomSd')
    if isinstance(gsd, str):
        gsd = float(gsd)

    if not _isnum(gmean) or not _isnum(gsd):
        logging.debug("Found invalid geometric mean/standard deviation!")
        return None

    u = o.Uncertainty.from_dict({
        'distributionType': o.UncertaintyType.LOG_NORMAL_DISTRIBUTION,
        'geomMean': gmean,
        'geomSd': gsd
    })

    return u


def _unit(unit_name):
    """Get the ID of the openLCA reference unit with the given name.

    Notes
    -----
    The version 4 UUIDs provided in this module are the same as those provided
    by GreenDelta's olca_schema.units sub-package, so it was replaced with
    their unit-reference method.

    Parameters
    ----------
    unit_name : str, dict
        If unit name is passed as a dictionary, it should have a key, "name"
        with a string value for the unit name (e.g., "MJ").

    Returns
    -------
    olca_schema.Ref
        A reference object for a Unit class.

    """
    if isinstance(unit_name, dict):
        try:
            unit_name = unit_name["name"]
        except KeyError:
            unit_name = ""
            logging.error(
                'dict passed as unit_name but does not contain name key')
    logging.debug("Creating unit, '%s'" % unit_name)
    r_obj = o_units.unit_ref(unit_name)
    if r_obj is None:
        logging.error("unknown unit, '%s'; no unit reference" % unit_name)
    return r_obj


def _update_data(cur_data, new_data):
    """Update a current data dictionary with new values.

    Parameters
    ----------
    cur_data : dict
        A data dictionary with UUIDs (ids) and olca root entities (objs) read
        from a JSON-LD zip archive (i.e., current data).
    new_data : dict
        A data dictionary with UUIDs (ids) and olca root entities (objs)
        processed by electricitylci.main; it may be the same or new values as
        already written to JSON-LD.

    Returns
    -------
    dict
        The current data updated with new values (i.e., overwrites existing
        values and appends new).
    """
    for k in cur_data.keys():
        # Make ids/obj lists to dict and update current data with new.
        d_cur = _make_entity_dict(cur_data, k)
        d_new = _make_entity_dict(new_data, k)
        d_cur.update(d_new)

        # Plop the new lists back into the data dictionary
        ids = []
        objs = []
        for uid, obj in d_cur.items():
            ids.append(uid)
            objs.append(obj)
        new_data[k]['ids'] = ids
        new_data[k]['objs'] = objs

    return new_data


def _val(dict_d, *path, **kvargs):
    """Return value from a dictionary.

    If a valid key (path) is provided to a given dictionary (dict_d), the
    respective value of the dictionary is returned; otherwise, kvargs is
    checked for a default value. If a default value is present, then it is
    returned; otherwise, NoneType is returned.

    Parameters
    ----------
    dict_d : dict
    path : tuple, optional
        A tuple of dictionary keys (str).
        Note: there should only ever be one path; for multiple paths only
        the first key will be accessed.
    kvargs : dict
        Used to store default values.
        See key 'default'.

    Returns
    -------
    Variable
        The value from a given key to a given dictionary.
        If not found, a NoneType is returned.

    Examples
    --------
    >>> d = {'a': 1, 'b': 2, 'c': 3}
    >>> _val(d, 'a') # single key value
    1
    >>> _val(d,'x') # NoneType for missing key
    None
    >>> _val(d, 'x', 'y' 'z', 'a', 'b', 'c') # first real key's value
    1
    >>> _val(d, 'z', default=4) # default for missing key
    4
    """
    r_val = None
    if isinstance(dict_d, dict) and path:
        for p in path:
            if p in dict_d.keys():
                r_val = dict_d[p]
                break  # HOTFIX: stop on first found key
    if r_val is None and 'default' in kvargs:
        r_val = kvargs['default']
    return r_val
