#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# solar_upstream.py
#
##############################################################################
# REQUIRED MODULES
##############################################################################
import logging
import os

import numpy as np
import pandas as pd

from electricitylci.globals import data_dir
from electricitylci.eia923_generation import eia923_download_extract


##############################################################################
# MODULE DOCUMENTATION
##############################################################################
__doc__ = """This module generates the annual emissions of each flow type for
the construction of each solar facility included in EIA 923 based on solely
the upstream contributions. Emissions from the construction of panels are
accounted for elsewhere.

Last updated:
    2024-10-16
"""
__all__ = [
    "generate_upstream_solar",
]


##############################################################################
# GLOBALS
##############################################################################
RENEWABLE_VINTAGE = 2016


##############################################################################
# FUNCTIONS
##############################################################################
def _solar_construction(year):
    """Generate solar PV construction inventory.

    The construction UP functional unit is normalized per year.

    Parameters
    ----------
    year : int
        The EIA generation year.

    Returns
    -------
    pandas.DataFrame
        Emissions inventory for solar PV construction.
        Returns NoneType for 2016 renewables vintage---the O&M emissions
        include the construction inventory.

    Raises
    ------
    ValueError
        If renewable vintage year is unsupported.
    """
    # Iss150, new construction and O&M LCIs
    if RENEWABLE_VINTAGE == 2020:
        solar_df = pd.read_csv(
            os.path.join(
                data_dir,
                "renewables",
                "2020",
                "solar_pv_construction_lci.csv"
                ),
            header=[0, 1],
            na_values=["#VALUE!", "#DIV/0!"],
        )
    elif RENEWABLE_VINTAGE == 2016:
        logging.debug(
            "The 2016 solar PV LCI does not separate construction and O&M.")
        return None
    else:
        raise ValueError("Renewable vintage %s undefined!" % RENEWABLE_VINTAGE)

    columns = pd.DataFrame(solar_df.columns.tolist())
    columns.loc[columns[0].str.startswith('Unnamed:'), 0] = np.nan
    columns[0] = columns[0].ffill()
    solar_df.columns = pd.MultiIndex.from_tuples(
        columns.to_records(index=False).tolist()
    )
    solar_df_t = solar_df.transpose()
    solar_df_t = solar_df_t.reset_index()

    new_columns = solar_df_t.loc[0,:].tolist()
    new_columns[0] = 'Compartment'
    new_columns[1] = 'FlowName'
    solar_df_t.columns = new_columns
    solar_df_t.drop(index=0, inplace=True)

    solar_df_t_melt = solar_df_t.melt(
        id_vars=['Compartment','FlowName'],
        var_name='plant_id',
        value_name='FlowAmount'
    )
    solar_df_t_melt = solar_df_t_melt.astype({'plant_id' : int})

    solar_generation_data = _solar_generation(year)
    solar_upstream = solar_df_t_melt.merge(
        right=solar_generation_data,
        left_on='plant_id',
        right_on='Plant Id',
        how='left'
    )
    solar_upstream.rename(
        columns={'Net Generation (Megawatthours)': 'quantity',},
        inplace=True
    )
    solar_upstream["Electricity"] = solar_upstream["quantity"]
    solar_upstream.drop(
        columns=[
            'Plant Id',
            'NAICS Code',
            'Reported Fuel Type Code',
            'YEAR',
            'Total Fuel Consumption MMBtu',
            'State',
            'Plant Name',
        ],
        inplace=True
    )
    # These emissions will later be aggregated with any inventory power plant
    # emissions because each facility has its own construction impacts.
    solar_upstream['stage_code'] = "solar_pv_const"
    solar_upstream['fuel_type'] = 'SOLAR'
    compartment_map = {
        'Air': 'air',
        'Water': 'water',
        'Energy': 'input'
    }
    solar_upstream['Compartment'] = solar_upstream['Compartment'].map(
        compartment_map)
    solar_upstream["Unit"] = "kg"
    solar_upstream["input"] = False

    return solar_upstream


def _solar_generation(year):
    """Return the EIA generation data for solar PV power plants.

    Parameters
    ----------
    year : int
        EIA generation year.

    Returns
    -------
    pandas.DataFrame
        EIA generation data for solar PV power plants.
    """
    eia_generation_data = eia923_download_extract(year)
    eia_generation_data['Plant Id'] = eia_generation_data[
        'Plant Id'].astype(int)

    column_filt = (eia_generation_data['Reported Fuel Type Code'] == 'SUN')
    df = eia_generation_data.loc[column_filt, :]

    return df


def _solar_om(year):
    """Generate the operations and maintenance LCI for solar PV power plants.
    For 2016 electricity baseline, this data frame includes the construction
    inventory.

    Parameters
    ----------
    year : int
        EIA generation year.

    Returns
    -------
    pandas.DataFrame
        Emission inventory for solar PV power plant O&M.

    Raises
    ------
    ValueError
        If the renwables vintage is not defined or a valid year.
    """
    # Iss150, new construction and O&M LCIs
    if RENEWABLE_VINTAGE == 2020:
        solar_df = pd.read_csv(
            os.path.join(
                data_dir,
                "renewables",
                "2020",
                "solar_pv_om_lci.csv"
                ),
            header=[0, 1],
            na_values=["#VALUE!", "#DIV/0!"],
        )
    elif RENEWABLE_VINTAGE == 2016:
        solar_df = pd.read_csv(
            os.path.join(
                data_dir,
                "renewables",
                "2016",
                "solar_pv_inventory.csv"
            ),
            header=[0,1],
        )
    else:
        raise ValueError("Renewable vintage %s undefined!" % RENEWABLE_VINTAGE)

    # Correct the columns
    columns = pd.DataFrame(solar_df.columns.tolist())
    columns.loc[columns[0].str.startswith('Unnamed:'), 0] = np.nan
    columns[0] = columns[0].ffill()
    solar_df.columns = pd.MultiIndex.from_tuples(
        columns.to_records(index=False).tolist()
    )

    solar_df_t = solar_df.transpose()
    solar_df_t = solar_df_t.reset_index()

    # Make facilities the columns
    new_columns = solar_df_t.loc[0,:].tolist()
    new_columns[0] = 'Compartment'
    new_columns[1] = 'FlowName'
    solar_df_t.columns = new_columns
    solar_df_t.drop(index=0, inplace=True)

    # Make the rows flows by facility
    solar_df_t_melt = solar_df_t.melt(
        id_vars=['Compartment','FlowName'],
        var_name='plant_id',
        value_name='FlowAmount'
    )
    solar_df_t_melt = solar_df_t_melt.astype({
        'plant_id' : int,
        'FlowAmount': float,
    })

    solar_generation_data = _solar_generation(year)
    solar_ops = solar_df_t_melt.merge(
        right=solar_generation_data,
        left_on='plant_id',
        right_on='Plant Id',
        how='left'
    )
    solar_ops.rename(columns={
        'Net Generation (Megawatthours)': 'Electricity'},
        inplace=True,
    )

    # Unlike the construction inventory, operations are on the basis of
    # per MWh, so in order for the data to integrate correctly with the
    # rest of the inventory, we need to multiply all inventory by electricity
    # generation (in MWh) for the target year.
    solar_ops["quantity"] = solar_ops["Electricity"]
    solar_ops["FlowAmount"] = solar_ops["FlowAmount"]*solar_ops["Electricity"]
    solar_ops.drop(
        columns=[
            'Plant Id',
            'NAICS Code',
            'Reported Fuel Type Code',
            'YEAR',
            'Total Fuel Consumption MMBtu',
            'State',
            'Plant Name',
            ],
        inplace=True
    )

    solar_ops['stage_code'] = "Power plant"
    solar_ops['fuel_type'] = 'SOLAR'
    compartment_map={
        'Air':'air',
        'Water':'water',
        'Energy':'input'
    }
    solar_ops['Compartment'] = solar_ops['Compartment'].map(compartment_map)
    solar_ops["Unit"] = "kg"
    solar_ops["input"] = False

    return solar_ops


def generate_upstream_solar(year):
    """
    Generate the annual emissions.

    For solar panel construction for each plant in EIA923. The emissions
    inventory file has already allocated the total emissions to construct panels
    and balance of system for the entire power plant over the assumed 30 year
    life of the panels. So the emissions returned below represent 1/30th of the
    total site construction emissions.

    Notes
    -----
    Depends on the data file, solar_pv_inventory.csv, which contains emissions
    and waste streams for each facility in the United States as of 2016.

    Parameters
    ----------
    year: int
        Year of EIA-923 fuel data to use.

    Returns
    ----------
    pandas.DataFrame
    """
    logging.info("Generating upstream solar PV inventories")
    solar_pv_cons = _solar_construction(year)
    solar_pv_ops = _solar_om(year)

    if solar_pv_cons is not None:
        solar_pv_df = pd.concat(
            [solar_pv_cons, solar_pv_ops],
            ignore_index=True
        )
    else:
        solar_pv_df = solar_pv_ops

    # Provide unique data source
    solar_pv_df["Source"] = "netlnrelsolarpv"

    # Set the electricity column as an input.
    solar_pv_df.loc[
        solar_pv_df["FlowName"]=="Electricity", "input"] = True
    # Set electricity resource units.
    solar_pv_df.loc[
        solar_pv_df["FlowName"]=="Electricity", "Unit"] = "MWh"

    # HOTFIX water as an input (Iss147).
    #   These are the negative water-to-water emissions.
    water_filter = (solar_pv_df['Compartment'] == 'water') & (
        solar_pv_df['FlowAmount'] < 0) & (
            solar_pv_df['FlowName'].str.startswith('Water'))
    solar_pv_df.loc[water_filter, 'input'] = True
    solar_pv_df.loc[water_filter, 'FlowAmount'] *= -1.0

    return solar_pv_df


##############################################################################
# MAIN
##############################################################################
if __name__=='__main__':
    from electricitylci.globals import output_dir
    year = 2020
    solar_upstream = generate_upstream_solar(year)
    solar_upstream.to_csv(f'{output_dir}/upstream_solar_{year}.csv')
