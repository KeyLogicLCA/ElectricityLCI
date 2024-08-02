#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# __init__.py
#
##############################################################################
# REQUIRED MODULES
##############################################################################
import logging
import os

import pandas as pd

import electricitylci.model_config as config
from electricitylci.globals import elci_version
from electricitylci.globals import output_dir
from electricitylci.utils import fill_default_provider_uuids


##############################################################################
# MODULE DOCUMENTATION
##############################################################################
__doc__ = """This module contains the main API functions to be used by the
end user.

Last updated:
    2024-08-02
"""
__version__ = elci_version


##############################################################################
# FUNCTIONS
##############################################################################
def add_fuels_to_gen(gen_df, fuel_df, canadian_gen, upstream_dict):
    """Add the upstream fuels to the generation dataframe as fuel inputs.

    Parameters
    ----------
    gen_df : pandas.DataFrame
        The generation dataframe, as generated by `get_gen_plus_netl`
        or `get_generation_process_df`.
    fuel_df : pandas.DataFrame
        The upstream process data frame (e.g., from `get_upstream_process_df`).
    canadian_gen : pandas.DataFrame
        The Canadian generation data frame (e.g., as returned by
        `combine_upstream_and_gen_df`).
    upstream_dict : dict
        This is the dictionary of upstream "unit processes" as generated by
        electricitylci.upstream_dict after the upstream_dict has been written
        to json-ld. This is important because the uuids for the upstream
        "unit processes" are only generated when written to json-ld.

    Returns
    -------
    pandas.DataFrame
    """
    import electricitylci.combinator as combine

    logging.info("Adding fuel inputs to generator emissions...")
    gen_plus_fuel = combine.add_fuel_inputs(gen_df, fuel_df, upstream_dict)

    logging.info("Adding Canadian generator emissions...")
    gen_plus_fuel = pd.concat([gen_plus_fuel, canadian_gen], ignore_index=True)

    return gen_plus_fuel


def aggregate_gen(gen_df, subregion="BA"):
    """Run aggregation routine to place all emissions and fuel inputs on the
    basis of a MWh generated at the power plant gate.

    This is in preparation for generating power plant unit processes for
    openLCA.

    Parameters
    ----------
    gen_df : pandas.DataFrame
        The generation dataframe as generated by get_gen_plus_netl
        or get_generation_process_df.
    subregion : str, optional
        The level of subregion that the data will be aggregated to. Choices
        are 'eGRID', 'NERC', 'FERC', 'BA', 'US'. Defaults to 'BA'.

    Returns
    -------
    pandas.DataFrame
    """
    import electricitylci.generation as gen

    if subregion is None:
        # This change has been made to accommodate the new method of generating
        # consumption mixes for FERC regions. They now pull BAs to provide
        # a more accurate inventory. The tradeoff here is that it's no longer
        # possible to make a FERC region generation mix and also provide the
        # consumption mix. Or it could be possible but would require running
        # through aggregate twice.
        subregion = "BA"
    logging.info(f"Aggregating to subregion - {subregion}")
    aggregate_df = gen.aggregate_data(gen_df, subregion=subregion)
    return aggregate_df


def combine_upstream_and_gen_df(gen_df, upstream_df):
    """
    Combine the generation and upstream dataframes into a single dataframe.
    The emissions represented here are the annual emissions for all power
    plants. This dataframe would be suitable for further analysis.

    Parameters
    ----------
    gen_df : pandas.DataFrame
        The generator dataframe, generated by get_gen_plus_netl or
        get_generation_process_df. Note that get_generation_process_df returns
        two dataframes. The intention would be to send the second returned
        dataframe (plant-level emissions) to this routine.
    upstream_df : pandas.DataFrame
        The upstream dataframe, generated by get_upstream_process_df

    Returns
    -------
    tuple
        A tuple of length two.
        The first item is the combined generation and upstream data frame.
        The second item is the Canadian generation data frame.
    """
    import electricitylci.combinator as combine
    import electricitylci.import_impacts as import_impacts

    logging.info("Combining upstream and generation inventories")
    combined_df = combine.concat_clean_upstream_and_plant(gen_df, upstream_df)
    # BUG: KeyError in 2021 data
    canadian_gen = import_impacts.generate_canadian_mixes(
        combined_df,
        config.model_specs.eia_gen_year)
    combined_df = pd.concat([combined_df, canadian_gen], ignore_index=True)
    return combined_df, canadian_gen


def get_consumption_mix_df(subregion=None, regions_to_keep=None):
    """Alternative to :func:`write_surplus_pool_and_consumption_mix_dict`.

    This function uses EIA trading data to calculate the consumption mix for
    balancing authority areas or FERC region. The aggregation choices are
    limited to these 2 because the data is available only at the balancing
    authority area.

    Parameters
    ----------
    subregion : str, optional
        Aggregation region (e.g., "BA" or "FERC"), by default None
    regions_to_keep : list, optional
        List of region names (e.g., balancing authority names), by default None

    Returns
    -------
    dict
        A dictionary with three keys: "BA", "FERC", and "US" for the
        three levels of consumption mix trading aggregation.
        The values of each key are pandas.DataFrames of trading data.
        The data frame columns report the import region, export region,
        transaction amount, total imports for import region, and fraction of
        total.
    """
    import electricitylci.eia_io_trading as trade

    if subregion is None:
        subregion = config.model_specs.regional_aggregation

    io_trade_df = trade.ba_io_trading_model(
        year=config.model_specs.NETL_IO_trading_year,
        subregion=subregion,
        regions_to_keep=regions_to_keep
    )
    return io_trade_df


def get_distribution_mix_df(subregion=None):
    """Generate transmission and distribution losses aggregated by subregion.

    Parameters
    ----------
    subregion : str, optional
        Subregion name, by default None

    Returns
    -------
    pandas.DataFrame
        A dataframe of transmission and distribution loss rates as a
        fraction.
    """
    import electricitylci.eia_trans_dist_grid_loss as tnd

    if subregion is None:
        subregion = config.model_specs.regional_aggregation

    td_loss_df = tnd.generate_regional_grid_loss(
        config.model_specs.eia_gen_year, subregion=subregion
    )
    return td_loss_df


def get_gen_plus_netl():
    """Combine the NETL life cycle data for solar, solar thermal,
    geothermal, wind, and hydro power plants with EIA 923 power plant data.

    Includes impacts (e.g., from construction) that would be omitted from the
    regular sources of emissions, then generates power plant emissions.
    The two different dataframes are combined to provide a single dataframe
    representing annual emissions or life cycle emissions apportioned over the
    appropriate number of years for all reporting power plants.

    Returns
    -------
    pandas.DataFrame
    """
    import electricitylci.combinator as combine
    import electricitylci.generation as gen
    import electricitylci.geothermal as geo
    import electricitylci.solar_upstream as solar
    import electricitylci.wind_upstream as wind
    import electricitylci.hydro_upstream as hydro
    import electricitylci.solar_thermal_upstream as solartherm

    eia_gen_year = config.model_specs.eia_gen_year
    logging.info(
        "Generating inventories for geothermal, solar, wind, hydro, and "
        "solar thermal..."
    )
    geo_df = geo.generate_upstream_geo(eia_gen_year)
    solar_df = solar.generate_upstream_solar(eia_gen_year)
    wind_df = wind.generate_upstream_wind(eia_gen_year)
    hydro_df = hydro.generate_hydro_emissions() # always 2016
    solartherm_df = solartherm.generate_upstream_solarthermal(eia_gen_year)

    # NOTE: hydro is purposefully left out here.
    logging.info("Concatenating renewable data frames")
    netl_gen = combine.concat_map_upstream_databases(
        eia_gen_year, geo_df, solar_df, wind_df, solartherm_df
    )
    netl_gen["DataCollection"] = 5
    netl_gen["GeographicalCorrelation"] = 1
    netl_gen["TechnologicalCorrelation"] = 1
    netl_gen["DataReliability"] = 1

    # Add hydro, which already has DQI information associated with it:
    logging.info("Concatenating hydro facilities with renewables")
    netl_gen = pd.concat(
        [netl_gen, hydro_df[netl_gen.columns]],
        ignore_index=True,
        sort=False
    )

    # This combines EIA 923, EIA 860, with EPA CEMS
    logging.info("Getting reported emissions for generators...")
    gen_df = gen.create_generation_process_df()

    combined_gen = combine.concat_clean_upstream_and_plant(gen_df, netl_gen)

    return combined_gen


def get_generation_mix_process_df(regions=None):
    """Create a data frame of generation mixes by fuel type in each subregion.

    This function imports and uses the parameter 'replace_egrid' and
    'gen_mix_from_model_generation_data' from model_config.py.

    If 'replace_egrid' is true or the specified 'regions' is true, then the
    generation mix will come from EIA 923 data.

    If 'replace_egrid' is false, then the generation mix will either come from
    the eGRID reference data ('gen_mix_from_model_generation_data' is false) or
    from the generation data from this model
    ('gen_mix_from_model_generation_data' is true).

    Parameters
    ----------
    regions : str, optional
        Regions to include.
        Defaults to 'all', which includes all eGRID subregions.

    Returns
    -------
    pandas.DataFrame
        A data frame of electricity generation by subregion by fuel category.
        Columns include: 'Subregion' (str), 'FuelCategory' (str),
        'Electricity' (float), and 'Generation_Ratio' (float).

    Examples
    --------
    >>> import electricitylci
    >>> import electricitylci.model_config as config
    >>> config.model_specs = config.build_model_class("ELCI_1")
    >>> all_gen_mix_db = electricitylci.get_generation_mix_process_df()
    >>> all_gen_mix_db.query("Subregion == 'JEA'")
        Subregion FuelCategory  Electricity  Generation_Ratio
    128       JEA      BIOMASS    40680.000          0.002869
    129       JEA         COAL  5035184.010          0.355156
    130       JEA          GAS  3687788.780          0.260118
    131       JEA        MIXED  5393144.008          0.380405
    132       JEA        SOLAR    20587.000          0.001452
    """
    from electricitylci.generation_mix import (
        create_generation_mix_process_df_from_model_generation_data,
    )
    from electricitylci.eia923_generation import build_generation_data

    if regions is None:
        regions = config.model_specs.regional_aggregation

    if config.model_specs.replace_egrid or regions in ["BA", "FERC", "US"]:
        if regions in ["BA","FERC","US"] and not (
                config.model_specs.replace_egrid):
            logging.info(
                "EIA923 generation data are being used for the generation mix "
                "despite replace_egrid = False. The reference eGrid "
                "electricity data cannot be reorganized to match BA or FERC "
                "regions. For the US region, the function for generating US "
                "mixes does not support aggregating to the US."
                )
        logging.info("EIA923 generation data are used when replacing eGRID")
        generation_data = build_generation_data(
            generation_years=[config.model_specs.eia_gen_year]
        )
        generation_mix_process_df = (
            create_generation_mix_process_df_from_model_generation_data(
                generation_data, regions)
        )
    else:
        from electricitylci.egrid_filter import (
            electricity_for_selected_egrid_facilities,
        )
        from electricitylci.generation_mix import (
            create_generation_mix_process_df_from_egrid_ref_data,
        )

        if config.model_specs.gen_mix_from_model_generation_data:
            generation_mix_process_df = create_generation_mix_process_df_from_model_generation_data(
                electricity_for_selected_egrid_facilities, regions
            )
        else:
            generation_mix_process_df = create_generation_mix_process_df_from_egrid_ref_data(
                regions
            )
    return generation_mix_process_df


def get_generation_process_df(regions=None, **kwargs):
    """Create emissions data frame from regional power generation by fuel type.

    When including upstream processes, this method also adds fuels to
    generation data (see :func:`add_fuels_to_gen`).

    "kwargs" include the upstream emissions dataframe (upstream_df) and
    dictionary (upstream_dict) if upstream emissions are being included.

    Parameters
    ----------
    regions : str, optional
        Regions to include in the analysis (the default is None, which uses
        the value read from YAML config file). Other options include
        "eGRID", "NERC", "BA", "US", "FERC", and "EIA".
    kwargs : dict, optional
        Optional additional arguments may include:

        - 'upstream_df' : pandas.DataFrame
        - 'upstream_dict' : dict
        - 'to_agg' : boolean

    Returns
    -------
    pandas.DataFrame
        Each row represents information about a single emission from a fuel
        category in a single region. Columns include the following.

       - 'Subregion' (str)
       - 'FuelCategory'
       - 'FlowName'
       - 'FlowUUID'
       - 'Compartment'
       - 'Year'
       - 'Source'
       - 'Unit'
       - 'ElementaryFlowPrimeContext' (str): either 'emission' or 'resource'
       - 'TechnologicalCorrelation'
       - 'TemporalCorrelation'
       - 'DataCollection'
       - 'Emission_factor'
       - 'Reliability_Score'
       - 'GeographicalCorrelation
       - 'GeomMean'
       - 'GeomSD'
       - 'Maximum'
       - 'Minimum'

    Examples
    --------
    >>> # The facility-level data method
    >>> data = get_generation_process_df(regions="BA", to_agg=False)
    >>> # The aggregated data method:
    >>> data = get_generation_process_df("BA")
    """
    # These packages depend on model_specs (order matters!)
    import electricitylci.generation as gen
    import electricitylci.combinator as combine

    # Add NETL renewables
    if config.model_specs.include_renewable_generation is True:
        generation_process_df = get_gen_plus_netl()
    else:
        generation_process_df = gen.create_generation_process_df()

    # Add NETL water
    if config.model_specs.include_netl_water is True:
        import electricitylci.plant_water_use as water

        water_df = water.generate_plant_water_use(
            config.model_specs.eia_gen_year)
        generation_process_df = combine.concat_clean_upstream_and_plant(
            generation_process_df, water_df)

    # Add upstream & Canadian processes
    if config.model_specs.include_upstream_processes is True:
        try:
            upstream_df = kwargs['upstream_df']
            upstream_dict = kwargs['upstream_dict']
        except KeyError:
            logging.info(
                "A kwarg named 'upstream_dict' must be included if "
                "include_upstream_processes is true."
            )
        # Get Canadian generation
        _, canadian_gen = combine_upstream_and_gen_df(
            generation_process_df, upstream_df
        )
        # Add upstream fuels and Canadian generation to plant gen
        gen_plus_fuels = add_fuels_to_gen(
            generation_process_df, upstream_df, canadian_gen, upstream_dict
        )
    else:
        import electricitylci.import_impacts as import_impacts

        # This change was made to accommodate the new method of generating
        # consumption mixes for FERC regions. They now pull BAs to provide
        # a more accurate inventory. The tradeoff here is that it's no longer
        # possible to make a FERC region generation mix and also provide the
        # consumption mix. Or it could be possible but would require running
        # through aggregate twice.
        canadian_gen_df = import_impacts.generate_canadian_mixes(
            generation_process_df,
            config.model_specs.eia_gen_year
        )
        generation_process_df = pd.concat(
            [generation_process_df, canadian_gen_df], ignore_index=True)
        gen_plus_fuels = generation_process_df

    # NOTE: It would be nice if the following were in a separate method so
    # gen_plus_fuels could be retained without aggregation.
    to_agg = kwargs.get("to_agg", True)
    if to_agg:
        if regions is None:
            regions = config.model_specs.regional_aggregation

        if regions in ["BA", "FERC", "US"]:
            generation_process_df = aggregate_gen(gen_plus_fuels, "BA")
        else:
            generation_process_df = aggregate_gen(gen_plus_fuels, regions)
    else:
        # WARNING, Canada is BA level. Probably not useful and should be
        # filtered out.
        generation_process_df = gen_plus_fuels

    return generation_process_df


def get_facility_level_inventory(to_save=False, sep_by_fac=True):
    """Returns a facility-level data frame and optionally writes to CSV.

    Parameters
    ----------
    to_save : bool, optional
        To write data as CSV, by default False
    sep_by_fac : bool, optional
        Whether to save by facility ID, by default True

    Returns
    -------
    pandas.DataFrame
        A data frame of facility-level inventories.
        The choice of model configuration (e.g., ELCI_1) determines whether
        upstream emissions, renewable technologies, and plant-level water
        are included. Ignores Canada.

        Columns include:

        - 'eGRID_ID'
        - 'FlowName'
        - 'Compartment'
        - 'FlowAmount'
        - 'Unit'
        - 'DataReliability'
        - 'Source'
        - 'Year'
        - 'PGM_SYS_ID'
        - 'plant_name'
        - 'PrimaryFuel'
        - 'DataCollection'
        - 'GeographicalCorrelation'
        - 'TechnologicalCorrelation'
        - 'Electricity'
        - 'NAICS Code'
        - 'PercentGenerationfromDesignatedFuelCategory'
        - 'State'
        - 'NERC'
        - 'Balancing Authority Code'
        - 'Balancing Authority Name'
        - 'FuelCategory'
        - 'SourceListName'
        - 'FlowUUID'
        - 'ElementaryFlowPrimeContext'
        - 'Age'
        - 'TemporalCorrelation'
        - 'stage_code'
        - 'Compartment_path'
        - 'EIA_Region'
        - 'FERC_Region'
        - 'quantity'
        - 'input'
        - 'FacilityID'
    """
    if config.model_specs is None:
        # Prompt user to select configuration option.
        # These are defined as YAML files in the modelconfig/ folder in the
        # eLCI package; you might have to search site-packages under lib.
        config.model_specs = config.build_model_class()

    # File naming convention for "no upstream"
    us_txt =  "nu"

    if config.model_specs.include_upstream_processes is True:
        # File naming convention for "upstream"
        us_txt = "us"

        # Create data frame with all generation process data; includes
        # upstream and Canadian data.
        # NOTE: Only nuclear ('NUC') stage codes have electricity data;
        #       all others are nans.
        logging.info("get upstream process")
        upstream_df = get_upstream_process_df(config.model_specs.eia_gen_year)
        logging.info("write upstream process to dict")
        upstream_dict = write_upstream_process_database_to_dict(upstream_df)

        # NOTE: UUID's for upstream processes are created when converting to
        #       JSON-LD. This has to be done here if the information is
        #       going to be included in the final outputs.
        upstream_dict = write_process_dicts_to_jsonld(upstream_dict)
    else:
        # Create data frame with only generation process data.
        upstream_dict = {}
        upstream_df = None

    # NOTE: This method triggers an input request for EPA data API key;
    #       see https://github.com/USEPA/ElectricityLCI/issues/207
    # NOTE: This method runs aggregation and emission uncertainty
    #       calculations.
    logging.info("get aggregated generation process")
    data = get_generation_process_df(
        upstream_df=upstream_df,
        upstream_dict=upstream_dict,
        to_agg=False  # test facility-level data retrieval
    )
    # Drop Canada and unknown facilities from data frame
    data = data.dropna(subset='FacilityID')

    if to_save:
        if sep_by_fac:
            logging.info("Saving individual facility-level inventories to CSV")
            # Create CSV files for each facility
            fac_list = data['FacilityID'].unique()
            for fac in fac_list:
                tmp = data.query("FacilityID == %f" % fac)
                csv_file = "elci_fac_%05d_%d_%s.csv" % (
                    fac, config.model_specs.eia_gen_year, us_txt
                )
                csv_path = os.path.join(output_dir, csv_file)
                tmp.to_csv(csv_path, index=False)
        else:
            # Create one massive CSV file
            logging.info("Saving facility-level inventories to CSV")
            csv_file = "elci_fac_all_%d_%s.csv" % (
                config.model_specs.eia_gen_year, us_txt)
            csv_path = os.path.join(output_dir, csv_file)
            data.to_csv(csv_path, index=False)
            # TODO: consider zipping this massive file, 500 MB.

    return data


def get_upstream_process_df(eia_gen_year):
    """Automatically load all of the upstream emissions data from the various
    modules.

    Parameters
    ----------
    eia_gen_year : int

    Returns
    -------
    pandas.DataFrame
        A data frame with upstream emissions from coal, natural gas, petroleum,
        nuclear, and plant construction.

        Columns include:

        - 'plant_id'
        - 'FuelCategory' (e.g., CONSTRUCTION, OIL, COAL, GAS)
        - 'stage_code': construction or basin (e.g., coal_const, Unita)
        - 'FlowName': emission name (e.g., 2,4-D diethanolamine salt)
        - 'Compartment' (e.g., emission/water)
        - 'Compartment_path (e.g., emission/water, resource, NaN)
        - 'FlowUUID' (str)
        - 'Unit' (str): emission unit
        - 'ElementaryFlowPrimeContext' (e.g., emission, resource, technosphere)
        - 'FlowAmount': emission amount
        - 'quantity': nameplate capacity for construction; fuel production?
        - 'Source (i.e., 'netl')
        - 'Year' (int): EIA generation year
        - 'Electricity': Available for nuclear only
        - 'input' (bool): for input flows (otherwise, emission)
        - 'stage': denotes 'Mining' and 'Transportation' (for coal only)
        - 'FlowType' (e.g. PRODUCT_FLOW, ELEMENTARY_FLOW, or WASTE_FLOW)
        - 'Basin': natural gas basin (for GAS only)
    """
    import electricitylci.coal_upstream as coal
    import electricitylci.natural_gas_upstream as ng
    import electricitylci.petroleum_upstream as petro
    import electricitylci.nuclear_upstream as nuke
    import electricitylci.power_plant_construction as ppc
    import electricitylci.combinator as combine

    logging.info("Generating upstream inventories...")
    coal_df = coal.generate_upstream_coal(eia_gen_year)
    ng_df = ng.generate_upstream_ng(eia_gen_year)
    petro_df = petro.generate_petroleum_upstream(eia_gen_year)
    nuke_df = nuke.generate_upstream_nuc(eia_gen_year)
    const = ppc.generate_power_plant_construction(eia_gen_year)
    upstream_df = combine.concat_map_upstream_databases(
        eia_gen_year, petro_df, nuke_df, const
    )
    # coal and ng already conform to mapping so no mapping needed
    upstream_df = pd.concat(
        [upstream_df, coal_df, ng_df], sort=False, ignore_index=True)
    return upstream_df


def run_epa_trade(gen_mix_df, gen_mix_dict, gen_process_dict):
    logging.info("us average mix to dict")
    usavegfuel_mix_dict = write_fuel_mix_database_to_dict(
        gen_mix_df,
        gen_process_dict
    )
    logging.info("write us average mix to jsonld")
    usavegfuel_mix_dict = write_process_dicts_to_jsonld(
        usavegfuel_mix_dict
    )
    logging.info("international average mix to dict")
    international_mix_dict = write_international_mix_database_to_dict(
        gen_mix_df,
        usavegfuel_mix_dict
    )
    international_mix_dict = write_process_dicts_to_jsonld(
        international_mix_dict
    )
    # Get surplus and consumption mix dictionary
    sur_con_mix_dict = write_surplus_pool_and_consumption_mix_dict()
    # Get dist dictionary
    dist_dict = write_distribution_dict()

    logging.info('write surplus pool consumption mix to jsonld')
    sur_con_mix_dict = write_process_dicts_to_jsonld(sur_con_mix_dict)

    logging.info('set default providers in surplus pool consumption mix')
    # BUG: KeyError, 'input' for exch
    sur_con_mix_dict = fill_default_provider_uuids(
        sur_con_mix_dict,
        sur_con_mix_dict,
        gen_mix_dict,
        international_mix_dict
    )
    sur_con_mix_dict = write_process_dicts_to_jsonld(sur_con_mix_dict)
    dist_dict = fill_default_provider_uuids(dist_dict, sur_con_mix_dict)
    dist_dict = write_process_dicts_to_jsonld(dist_dict)

    return dist_dict


def run_net_trade(generation_mix_dict):
    """Net trading method.

    Uses EIA trading data between balancing authority areas to calculate
    net trades based on an annual basis.

    Parameters
    ----------
    generation_mix_dict : dict
        Dictionary created by :func:`write_generation_mix_database_to_dict`.

    Returns
    -------
    dict
        A dictionary of three keys, "BA", "FERC", and "US".
        Each key's value represents its regions (e.g., 54 BAs, 10 FERC,
        and 1 US) as a dictionary. Each of the values of these keys is
        another dictionary, which represents an olca-schema process for
        the consumption mix representing electricity distribution to the
        end user within the named region with a 1 MWh of electricity as
        its product flow.
    """
    # Run ba_io_trading.
    logging.info("using alt gen method for consumption mix")
    regions_to_keep = list(generation_mix_dict.keys())
    cons_mix_df_dict = get_consumption_mix_df(regions_to_keep=regions_to_keep)

    logging.info("write consumption mix to dict")
    cons_mix_dicts={}
    for subreg in cons_mix_df_dict.keys():
        cons_mix_dicts[subreg] = write_consumption_mix_to_dict(
            cons_mix_df_dict[subreg],
            generation_mix_dict,
            subregion=subreg
        )

    logging.info("write consumption mix to jsonld")
    for subreg in cons_mix_dicts.keys():
        cons_mix_dicts[subreg] = write_process_dicts_to_jsonld(
            cons_mix_dicts[subreg]
        )

    logging.info("get t&d losses")
    dist_mix_df_dict = {}
    for subreg in cons_mix_dicts.keys():
        dist_mix_df_dict[subreg] = get_distribution_mix_df(subregion=subreg)

    # NOTE: fails to find 'New Smyrna Beach' and 'City of Homestead'
    logging.info("write dist mix to dict")
    dist_mix_dicts = {}
    for subreg in dist_mix_df_dict.keys():
        dist_mix_dicts[subreg] = write_distribution_mix_to_dict(
            dist_mix_df_dict,
            cons_mix_dicts,
            subregion=subreg
        )

    logging.info("write dist mix to jsonld")
    for subreg in dist_mix_dicts.keys():
        dist_mix_dicts[subreg] = write_process_dicts_to_jsonld(
            dist_mix_dicts[subreg]
        )

    return dist_mix_dicts


def run_post_processes():
    """Run post processes on JSON-LD file.

    There is no easy way of editing files archived in a zip and no
    easy way of remove files from a zip without just creating a new zip,
    so that's what's done here.

    1. There should be only one instance of each .json file;
       addressed in the new _save_to_json (olca_jsonld_writer.py)
    2. Remove flows/*.json that are not found in a process exchange.
       See cleanup SQL queries in GitHub issue
       https://github.com/USEPA/ElectricityLCI/issues/216
    3. Remove zero flows from quantitative reference exchanges
       https://github.com/USEPA/ElectricityLCI/issues/217
    4. DO NOT ADD NETL TRACI 2.1 characterization factors
    5. Create product systems for select processes (user, consumption mixes)
    """
    from electricitylci.olca_jsonld_writer import build_product_systems
    from electricitylci.olca_jsonld_writer import clean_json

    clean_json(config.model_specs.namestr)
    build_product_systems(
        file_path=config.model_specs.namestr,
        elci_config=config.model_specs.model_name
    )


def write_consumption_mix_to_dict(cons_mix_df, dist_mix_dict, subregion=None):
    import electricitylci.eia_io_trading as trade

    if subregion is None:
        subregion = config.model_specs.regional_aggregation

    cons_mix_dict = trade.olca_schema_consumption_mix(
        cons_mix_df, dist_mix_dict, subregion=subregion
    )
    return cons_mix_dict


def write_distribution_dict():
    """Create an openLCA schema dictionary of consumption mix processes for
    each eGRID subregion that accounts for electricity losses during
    transmission and distribution based on the YAML-configured value of
    'efficiency of distribution grid.'

    Returns
    -------
    dict
        An openLCA formatted dictionary of distribution mix processes for each
        eGRID subregion.
    """
    from electricitylci.distribution import distribution_mix_dictionary

    return distribution_mix_dictionary()


def write_distribution_mix_to_dict(dm_dict, gm_dict, subregion=None):
    import electricitylci.eia_trans_dist_grid_loss as tnd

    if subregion is None:
        subregion = config.model_specs.regional_aggregation

    # HOTFIX: send full dicts to method, not region-specific [2024-03-21;TWD]
    dist_mix_dict = tnd.olca_schema_distribution_mix(
        dm_dict, gm_dict, subregion=subregion
    )
    return dist_mix_dict


def write_fuel_mix_database_to_dict(genmix_db, gen_dict, regions=None):
    from electricitylci.generation_mix import olcaschema_usaverage

    if regions is None:
        regions = config.model_specs.regional_aggregation
    if regions in ["FERC","US","BA"]:
        regions = "BA"
    usaverage_dict = olcaschema_usaverage(genmix_db, gen_dict, regions)

    return usaverage_dict


def write_gen_fuel_database_to_dict(
        gen_plus_fuel_df, upstream_dict, subregion=None):
    """Write the generation dataframe that was augmented with fuel inputs
    to a dictionary for conversion to openLCA.

    Parameters
    ----------
    gen_plus_fuel_df : pandas.DataFrame
        The dataframe returned by add_fuels_to_gen
    upstream_dict : dict
        This is the dictionary of upstream "unit processes" as generated by
        electricitylci.upstream_dict after the upstream_dict has been written
        to json-ld. This is important because the uuids for the upstream
        "unit processes" are only generated when written to json-ld.
    subregion : str, optional
        The level of subregion that the data will be aggregated to. Choices
        are 'all', 'NERC', 'BA', 'US'. Defaults to 'BA'.

    Returns
    -------
    dict
        A dictionary of generation unit processes ready to be written to
        openLCA.
    """
    import electricitylci.generation as gen

    if subregion is None:
        # Another change to accommodate FERC consumption pulling BAs.
        subregion = config.model_specs.regional_aggregation

    # TODO:
    # Removing the statements below for now. This is preventing the generation
    # of dictionaries for other levels of aggregation. This logic will need to
    # be implemented in main.py so that FERC consumption mixes can be made
    # using the required BA aggregation.
    # if subregion in ["BA","FERC","US"]:
    #     subregion="BA"
    logging.info("Converting generator dataframe to dictionaries...")
    gen_plus_fuel_dict = gen.olcaschema_genprocess(
        gen_plus_fuel_df, upstream_dict, subregion=subregion
    )
    return gen_plus_fuel_dict


def write_generation_mix_database_to_dict(genmix_db, gen_dict, regions=None):
    """Create olca-formatted dictionaries for the data frame returned by
    :func:`get_generation_mix_process_df`.

    Must pass both the generation mix data frame and the dictionary from
    :func:`write_generation_process_database_to_dict` to properly create the
    links to the power plant types.

    Parameters
    ----------
    genmix_db : pandas.DataFrame
        A generation mix data frame (e.g., from `get_generation_mix_df`).
    gen_dict : dict
        An olca-schema-formatted process dictionary used as a reference.
    regions : str, optional
        Region aggregation level (e.g., 'BA'), by default None.
        If none, the regional aggregation level from the configuration file
        is used.

    Returns
    -------
    dict
        An olca-schema-formatted process dictionary for generation mixes.
    """
    from electricitylci.generation_mix import olcaschema_genmix

    if regions is None:
        regions = config.model_specs.regional_aggregation
    if regions in ["FERC","US","BA"]:
        regions = "BA"
    genmix_dict = olcaschema_genmix(genmix_db, gen_dict, regions)

    return genmix_dict

# TODO: find if/where this is called.
def write_generation_process_database_to_dict(gen_database, regions=None):
    """Create olca-formatted dictionaries of individual processes.

    Parameters
    ----------
    gen_database : pandas.DataFrame
        Each row represents information about a single emission from a fuel
        category in a single region.
    regions : str, optional
        Not currently used.
        Defaults to None.

    Returns
    -------
    dict
        A dictionary of dictionaries, each of which contains information about
        emissions from a single fuel type in a single region.
    """
    import electricitylci.generation as gen

    if regions is None:
        regions = config.model_specs.regional_aggregation

    gen_dict = gen.olcaschema_genprocess(gen_database, subregion=regions)

    return gen_dict


def write_international_mix_database_to_dict(genmix_db, us_mix, regions=None):
    from electricitylci.generation_mix import olcaschema_international

    if regions is None:
        regions = config.model_specs.regional_aggregation
    if regions in ["FERC","US","BA"]:
        regions = "BA"
    international_dict = olcaschema_international(
        genmix_db, us_mix, subregion=regions
    )

    return international_dict


def write_process_dicts_to_jsonld(*process_dicts):
    """Send one or more process dictionaries to be written to JSON-LD.

    Parameters
    ----------
    process_dicts : tuple
        Unpacked variable arguments.
        Each instance of process_dicts should be a dictionary.
        See https://peps.python.org/pep-0448/

    Returns
    -------
    dict
    """
    from electricitylci.olca_jsonld_writer import write

    all_process_dicts = dict()
    for d in process_dicts:
        # Append dictionaries together using double asterisk syntax
        # (see about dictionary interaction with ** syntax)
        # Basically, the asterisk in this method turns the dictionary of
        # dictionaries into a tuple of dictionaries, and here we are putting
        # it all back to a single dictionary!
        all_process_dicts = {**all_process_dicts, **d}
    olca_dicts = write(all_process_dicts, config.model_specs.namestr)
    logging.info("Wrote JSON-LD to %s" % config.model_specs.namestr)
    return olca_dicts


def write_surplus_pool_and_consumption_mix_dict():
    """Create olca formatted dictionaries for the consumption mix as
    calculated by consumption_mix.py.

    Note that this funcion directly pulls the dataframes, converts the data
    into the dictionary, and then returns the dictionary.

    Returns
    -------
    dict
        The surplus pool and consumption mixes for the various regions.
    """
    from electricitylci.consumption_mix import surplus_dict
    from electricitylci.consumption_mix import consumption_dict

    surplus_pool_and_con_mix = {**surplus_dict, **consumption_dict}
    return surplus_pool_and_con_mix


def write_upstream_process_database_to_dict(upstream_df):
    """Convert upstream dataframe generated by get_upstream_process_df to
    dictionaries to be written to json-ld.

    Parameters
    ----------
    upstream_df : pandas.DataFrame
        Combined dataframe as generated by gen_upstream_process_df.

    Returns
    -------
    dict
    """
    import electricitylci.upstream_dict as upd

    logging.info("Writing upstream processes to dictionaries")
    upstream_dicts = upd.olcaschema_genupstream_processes(upstream_df)
    return upstream_dicts
