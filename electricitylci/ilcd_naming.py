# [FH] Work in Progress
# this is a function to rename processes in the ILCD format
import re
import logging

from sys import prefix
from netlolca import get_dict_number
import olca_schema as o


def apply_ilcd_naming(p_name, p_location):
    """
    This function reads a dictionary of processes and applies ILCD naming 
    conventions to the process names. The function identifies the appropriate 
    renaming function based on the process name and modifies the process name 
    in place. Process categories with different naming functions include:
        * Consumption mix at grid
        * Consumption mix at user
        * Generation at source
        * Generation mix 
        * Plant Construction
        * Fuel production
        * Natural gas extraction, processing and transport

    The general ILCD naming convention is as follows:
    "Base name; treatment received, production route(s), standard(s) fulfilled; 
        production or consumption type, location type; quantitative flow properties"
    
    The naming convention applied slightly varies based on the type of 
    process and is highlighted in the documentation of each renaming function.  

    Parameters:
        elci_dict (dict): A dictionary containing process information, including 
        process names and locations.
    
    Returns:
        None: The function modifies the input dictionary in place, renaming the 
        processes according to ILCD conventions.
    """
    renaming_f = get_renaming_function(p_name)
    if renaming_f:
        p_name = renaming_f(p_name, p_location)
    else:
        logging.warning(f"No renaming function found for process: {p_name}." 
                        f"Process name will remain unchanged.")

    return p_name

# Helper Function - Get appropriate renaming function based on process name
###########################################################################
def get_renaming_function(name):
    """
    This function returns the appropriate renaming function based on the process name. 
    A dictionary of renaming functions is defined, where the keys are the prefixes of 
    the process names and the values are the corresponding renaming functions.
    
    Parameters:
        name (str): The name of the process to be renamed.
    
    Returns:
        function: The renaming function corresponding to the process name prefix, 
        or None if no match is found.    
    """
    renaming_functions = {
        "Electricity; at grid; consumption mix": rename_consumption_mix_at_grid,
        "Electricity; at grid; residual consumption mix": rename_consumption_mix_at_grid,
        "Electricity; at user; consumption mix": rename_consumption_mix_at_user,
        "Electricity; at user; residual consumption mix": rename_consumption_mix_at_user,
        "Electricity - ": rename_generation_at_source,
        "Electricity; at grid; generation mix -": rename_generation_mix,
        "power plant construction - ": rename_plant_construction,
        "coal extraction and processing - ": rename_coal_processes,
        "coal transport - ": rename_coal_transport_processes,
        "petroleum extraction and processing - ": rename_petroleum_production,
        "nuclear fuel extraction, processing, and transport": rename_nuclear_production,
        "natural gas extraction, processing, and transport - ": rename_natural_gas_processes
    }

    return next(
        (
            func
            for prefix, func in renaming_functions.items()
            if name.startswith(prefix)
        ),
        None,
    )

# Helper Functions - Renaming functions
###########################################################################
def rename_consumption_mix_at_grid(p_name, location):
    """
  	This is the renaming function for electricity consumption mix at grid 
    processes. The main elements in the final name are:
        * Consumption mix setting - Consumption mix or Residual consumption mix
        * Regional Aggregation - eGRID, NERC, BA, US, FERC, EIA
        * Region - the location of the process - retrieved from the process 
                location attribute
    
    Final name format:
        Electricity grid mix; {Consumption mix setting}; at grid; {Regional 
            Aggregation}; {Region}
        Example:
            Electricity grid mix; Consumption mix; at grid; FERC; Southeast
    
    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine the region.
    
    Returns:
        str: The renamed process name in ILCD format.
    
    Example:
        In[1]:  'Electricity; at grid; consumption mix - Alcoa Power Generating, 
                Inc. - Yadkin Division - BA - Alcoa Power Generating, Inc. - Yadkin 
                Division'
        Out[2]: 'Electricity grid mix; Consumption mix; at grid; BA; Alcoa Power 
                Generating, Inc. - Yadkin Division'
    """
    try:
        cons_mix_setting = (
            "Residual consumption mix" 
            if "residual" in p_name.lower() 
            else "Consumption mix"
        )
    except Exception as e:
        logging.warning(f"Error determining consumption mix setting for process: "
                        f"{p_name} - defaulting to 'Consumption mix'.")
        cons_mix_setting = "Consumption mix"

    try:
        regional_aggregation = next(
            (option for option in ["eGRID", "NERC", "BA", "US", "FERC", "EIA"]
            if option.lower() in p_name.lower()),
            ""
        )
    except Exception as e:
        logging.warning(f"Error determining regional aggregation for process: "
                        f"{p_name} - regional aggregation will be excluded.")
        regional_aggregation = ""

    region = location
    try:
        new_n = (
            f"Electricity grid mix; {cons_mix_setting}; at grid; " 
            f"{regional_aggregation}; {region}"
        )
    except Exception as e:
        logging.error(f"Error renaming process: {p_name} - maintaining original name.")
        new_n = p_name

    return new_n

def rename_consumption_mix_at_user(p_name, location):
    """
    This method renames electricity consumption mix at user processes to follow the
    ILCD naming convention. The main elements in the final name are:
        * Consumption mix setting - Consumption mix or Residual consumption mix
        * Regional Aggregation - eGRID, NERC, BA, US, FERC, EIA
        * Region - the location of the process - retrieved from the process 
                location attribute

    Final name format:
        Electricity grid mix; {Consumption mix setting}; at user; {Regional 
            Aggregation}; {Region}; 120 V, AC
        Example:
            Electricity grid mix; Consumption mix; at user; FERC; Southeast; 120 V, AC

    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine the region.
    
    Returns:
        str: The renamed process name in ILCD format.
    
    Example:
        In[1]:  'Electricity; at user; residual consumption mix - Alcoa Power 
                Generating, Inc. - Yadkin Division - BA - Alcoa Power Generating, 
                Inc. - Yadkin Division'
        Out [2] 'Electricity grid mix; Residual consumption mix; at user; BA; Alcoa 
                Power Generating, Inc. - Yadkin Division; 120 V, AC'
    """    
    try:
        cons_mix_setting = (
            "Residual consumption mix" 
            if "residual" in p_name.lower() 
            else "Consumption mix"
        )
    except Exception as e:
        logging.warning(
            f"Error determining consumption mix setting for process: "
            f"{p_name} - defaulting to 'Consumption mix'."
        )
        cons_mix_setting = "Consumption mix"

    try:
        regional_aggregation = next(
            (option for option in ["eGRID", "NERC", "BA", "US ", "FERC", "EIA"]
            if option.lower() in p_name.lower()),
            ""
        )
    except Exception as e:
        logging.warning(
            f"Error determining regional aggregation for process: "
            f"{p_name} - regional aggregation will be excluded."
        )
        regional_aggregation = ""

    region = location

    try:
        new_n = (
            f"Electricity grid mix; {cons_mix_setting}; at user; "
            f"{regional_aggregation}; {region}; 120 V, AC"
        )
    except Exception as e:
        logging.error(
            f"Error renaming process: {p_name} - maintaining original name."
        )
        new_n = p_name

    return new_n

def rename_generation_at_source(p_name, location):
    """
    This method renames electricity generation at source processes to follow 
    the ILCD naming convention. The main elements in the final name are:
        * Operator - the operator of the electricity generation source 
        (e.g., Midcontinent Independent System Operator)
    
    Final name format:
        Electricity generation; at grid; {location_operator}; high-voltage
        Example:
            Electricity generation; at grid; Midcontinent Independent 
            System Operator; high-voltage
    
    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine 
            the operator.    
    
    Returns:
        str: The renamed process name in ILCD format.
    
    Example:
        In[1]:  "Electricity - BIOMASS - Arizona Public Service Company - 
                Arizona Public Service Company"
        Out[2]: 'Electricity generation; biomass; Arizona Public Service 
                Company; high-voltage'
    """
    try:
        source = p_name.split("-")[1].strip().lower()
    except Exception as e:
        logging.warning(
            f"Error extracting source from process name: {p_name} "
            f"- defaulting to 'unspecified source'."
        )
        source = "unspecified source"

    sources_map = {
        "biomass": "Biomass power plant",
        "gas": "Natural gas-fired power plant",
        "mixed": "Mixed-powered electricity",
        "othf": "Other fuel power plant",
        "coal": "Coal-fired power plant",
        "geothermal": "Geothermal power plant",
        "hydro": "Hydroelectric power plant",
        "natural gas": "Natural gas-fired power plant",
        "nuclear": "Nuclear power plant",
        "oil": "Oil-fired power plant",
        "solar": "Solar photovoltaic farm",
        "wind": "Wind farm",
        "solarthermal": "Solar thermal power plant", 
        "all": "All",
    }

    source = sources_map.get(source, source)

    if location is None:
        operator = "unspecified operator" 
    else:
        operator = location

    try: 
        new_n = f"Electricity generation; {source}; {operator}; high-voltage"
    except Exception as e:
        logging.error(
            f"Error renaming process: {p_name} - maintaining original name."
        )
        new_n = p_name

    return new_n

def rename_generation_mix(p_name, location):
    """
    This method renames electricity generation mix processes to follow the 
    ILCD naming convention. The main elements in the final name are:
        * Generation mix setting - Generation mix or Residual generation mix
        * Region - the location of the process - retrieved from the process 
            location attribute
    
    Final name format:
        Electricity grid mix; {generation mix setting}; at grid; {Region}
        Example:
            Electricity grid mix; Generation mix; at grid; FERC; Southeast        
    
    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine the 
                region.
    
    Returns:
        str: The renamed process name in ILCD format.
    
    Example
        In[1]:  "Electricity; at grid; generation mix - Avangrid Renewables, LLC 
                - Avangrid Renewables, LLC"
        Out[2]: 'Electricity grid mix; Generation mix; at grid; Avangrid Renewables, 
                LLC'
    """
    try:
        generation_mix_setting = (
            "Residual generation mix" 
            if "residual" in p_name.lower() 
            else "Generation mix"
        )
    except Exception as e:
        logging.warning(
            f"Error determining generation mix setting for process: "
            f"{p_name} - defaulting to 'Generation mix'."
        )
        generation_mix_setting = "Generation mix"

    if location is None:
        region = "unspecified region"  
    else:
        region = location

    try:    
        new_n = (
            f"Electricity grid mix; {generation_mix_setting}; at grid; {region}"
        )
    except Exception as e:
        logging.error(
            f"Error renaming process: {p_name} - maintaining original name."
        )
        new_n = p_name
    
    return new_n  

def rename_coal_processes(p_name, location):
    """
 	This method renames coal extraction and processing processes to follow the 
    ILCD naming convention. The main elements in the final name are:
        * Fuel name - the type of coal (e.g., bituminous coal, subbituminous coal, 
                    lignite coal, anthracite coal)
        * Treatment received - the type of treatment received (e.g., extraction, 
                    extraction and processing)
        * Production route - the production route (e.g., beneficiation, mining)
        * Location - the location of the process - retrieved from the process 
                    location attribute
    
    Final name format:
        {Fuel name}; {treatment received}; {production route}; {location}
        Example:
            bituminous coal; extraction and processing; beneficiation; United States

    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine the region.
    
    Returns:
        str: The renamed process name in ILCD format.
    
    Note - this method was developed using GenAI
    
    Example:
        In[1]:  'coal extraction and processing - West/Northwest, SUB, Underground 
                - West/Northwest'
        Out[2]: 'Subbituminous coal; extraction and processing; Underground mining; 
                West/Northwest'
    """
    coal_types = {
    "BIT": "Bituminous coal",
    "SUB": "Subbituminous coal",
    "LIG": "Lignite coal",
    "ANT": "Anthracite coal",
    }

    production_routes = {
    "Surface": "Surface mining",
    "Underground": "Underground mining",
    "Processing": "Beneficiation",
    }

    # Normalize spacing
    p_name = re.sub(r"\s+", " ", p_name).strip()

    # Separate the activity from its identifying attributes
    parts = re.split(r"\s*-\s*", p_name, maxsplit=1)

    if len(parts) != 2:
        raise ValueError(
            f"Unexpected coal process-name format: {p_name!r}"
        )
    
    activity, attributes = parts

    attribute_parts = [
        value.strip()
        for value in attributes.split(",")
        if value.strip()
    ]   

    if len(attribute_parts) < 3:
        raise ValueError(
            f"Expected location, coal type, and route in: {p_name!r}"
        )

    coal_code = attribute_parts[1].upper()
    route_code = attribute_parts[2].split("-")[0].strip()

    fuel_name = coal_types.get(
        coal_code,
        f"{coal_code.lower()} coal",
    )

    treatment_received = re.sub(
        r"^coal\s+",
        "",
        activity,
        flags=re.IGNORECASE,
    ).strip().lower()

    production_route = production_routes.get(
        route_code,
        route_code.lower(),
    )

    try:
        new_n = (
                f"{fuel_name} {treatment_received}; {production_route}; {location}"
            )
    except Exception as e:
        logging.error(
            f"Error renaming process: {p_name} - maintaining original name."
        )
        new_n = p_name

    return new_n

def rename_coal_transport_processes(p_name, location):
    """
    This method renames coal transport processes to follow the ILCD 
    naming convention. The main elements in the final name are:
        * Production route - the transport mode (e.g., rail, truck, barge)
        * Location - the location of the process - retrieved from the process 
                    location attribute
    
    Final name format:
        coal; transport; {production route}; {location}
        Example:
            coal; transport; rail; United States

    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine the 
                        region.
    
    Returns:
        str: The renamed process name in ILCD format.

    Example:
        In[1]:  'coal transport - rail - United States of America (the)'
        Out[2]: 'coal; transport; rail; United States'
    """
    p_name = re.sub(r"\s+", " ", p_name).strip()

    parts = [
        part.strip()
        for part in re.split(r"\s+-\s+", p_name)
    ]

    activity, transport_mode = parts

    if activity.lower() != "coal transport":
        raise ValueError(
            f"Expected a coal transport process, received: {activity!r}"
        )

    if location == "United States of America (the)":
        location = "United States"

    try:
        new_n = (
            f"Coal transport; {transport_mode}; {location}"
        )
    except Exception as e:
        logging.error(
            f"Error renaming process: {p_name} - maintaining original name."
        )
        new_n = p_name

    return new_n

def rename_petroleum_production(p_name, location):
    """
    This method renames petroleum extraction and processing processes to follow 
    the ILCD naming convention. The main elements in the final name are:
        * Fuel name - the type of petroleum fuel (e.g., distillate fuel oil, 
                    residual fuel oil)
        * Treatment received - the type of treatment received (e.g., extraction 
                    and processing)
        * Production route - the production route (e.g., petroleum production, 
                    PADD {number})
        * Location - the location of the process - retrieved from the process 
                    location attribute
    
    Final name format:
        {Fuel name}; {treatment received}; {production route}; {location}
    
    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine the region.
    
    Returns:
        str: The renamed process name in ILCD format.
    
    Example:
        In[1]:  'petroleum extraction and processing - DFO PADD 3 - United States 
                of America (the)'
        Out[2]: 'Distillate fuel oil; extraction and processing; Petroleum production, 
                PADD 3; United States'
    """
    fuel_types = {
        "DFO": "distillate fuel oil",
        "RFO": "residual fuel oil",
    }

    if location == "United States of America (the)":
        location = "United States"

    p_name = re.sub(r"\s+", " ", p_name).strip()

    # split into activity, PADD designation, and location
    parts = [
        part.strip()
        for part in re.split(r"\s+-\s+", p_name)
    ]

    activity, fuel_region = parts

    # Extract DFO/RFO and PADD number
    match = re.fullmatch(
        r"(DFO|RFO)\s+PADD\s+([1-5])",
        fuel_region,
        flags=re.IGNORECASE,
    )

    if not match:
        raise ValueError(
            f"Unexpected fuel/PADD designation: {fuel_region!r}"
        )

    fuel_code = match.group(1).upper()
    padd_number = match.group(2)

    fuel_name = fuel_types[fuel_code]

    treatment_received = re.sub(
        r"^petroleum\s+",
        "",
        activity,
        flags=re.IGNORECASE,
    ).strip().lower()

    production_route = f"Petroleum production, PADD {padd_number}"
    try:
        new_n = (
            f"{fuel_name} {treatment_received}; {production_route}; {location}"
        )

        new_n = new_n[0].upper() + new_n[1:]  # Capitalize first letter
    except:
        logging.error(
            f"Error renaming process: {p_name} - maintaining original name."
        )
        new_n = p_name

    return new_n

def rename_nuclear_production(p_name, location):
    """
    This method renames nuclear fuel extraction, processing, and transport 
    processes to follow the ILCD naming convention. The main element to be 
    retrieved in order to rename the process is the location of the process.

    Final name format:
        Nuclear fuel; extraction, processing, and transport; {location}
        Example:
            Nuclear fuel; extraction, processing, and transport; United States  
    
    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine 
            the region.  
    Returns:
        str: The renamed process name in ILCD format.

    Example:
        In[1]:  'nuclear fuel extraction, processing, and transport - United 
                States of America (the)'
        Out[2]: 'Nuclear fuel; extraction, processing, and transport; United States'
    """
    if location == "United States of America (the)":
        location = "United States"

    try:
        new_n = (
            f"Nuclear fuel extraction, processing, and transport; {location}"
        )

    except Exception as e:
        logging.error(
            f"Error renaming nuclear production process: {p_name} - {e}"
        )
        new_n = p_name

    return new_n

def rename_plant_construction(p_name, location):
    """
    This method renames power plant construction processes to follow the ILCD 
    naming convention. plants_map is a dictionary that maps plant construction 
    codes to their corresponding plant types and specifications.
    
    The main elements in the final name are:
        * Type of power plant - the type of power plant (e.g., Natural gas, Coal, 
            Oil, Solar, Wind, Nuclear, Hydroelectric)
        * Plant specification - the specification of the power plant (e.g., 
            combined cycle, photovoltaic)
        * Capacity - the capacity of the power plant (e.g., 50 MW)
        * Operator - the operator of the power plant (e.g., Arizona public service 
            company)
    
    Final name format:
        {Type of power plant}; power plant construction; {plant specification}; 
            {capacity}; {operator}
        Example:
            Natural gas; power plant construction; combined cycle; 50 MW; 
                Arizona public service company
    
    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine the operator.
    
    Returns:
        str: The renamed process name in ILCD format.

    Example:
        In[1]:  'power plant construction - coal_const - Dominion Energy South 
                Carolina, Inc. - Dominion Energy South Carolina, Inc.'
        Out[2]: 'Coal; power plant construction; unspecified; Dominion Energy 
                South Carolina, Inc.'
    """

    plants_map = {
        "ngcc_const": {
            "plant_type": "Natural gas",
            "plant_specification": "combined cycle",
        },
        "coal_const": {
            "plant_type": "Coal",
            "plant_specification": "unspecified",
        },
        "oil_const": {
            "plant_type": "Oil",
            "plant_specification": "unspecified",
        },
        "solar_pv_const": {
            "plant_type": "Solar",
            "plant_specification": "photovoltaic",
        },
        "wind_const": {
            "plant_type": "Wind",
            "plant_specification": "unspecified",
        },
        "nuclear_const": {
            "plant_type": "Nuclear",
            "plant_specification": "unspecified",
        },
        "hydro_const": {
            "plant_type": "Hydroelectric",
            "plant_specification": "unspecified",
        },
        "solar_thermal_const": {
            "plant_type": "Solar",
            "plant_specification": "thermal",
        },
    }

    plant_code = re.split(r"\s+-\s+", p_name.strip(), maxsplit=2)[1]

    plant_code = plant_code.lower()

    if plant_code not in plants_map:
        raise ValueError(
            f"Unknown power plant construction code: {plant_code!r}"
        )

    plant_info = plants_map[plant_code]

    plant_type = plant_info["plant_type"]
    plant_specification = plant_info["plant_specification"]
    try:
        new_n = (
            f"{plant_type} power plant construction; {plant_specification}; {location}"
        )    
    except Exception as e:
        logging.error(
            f"Error renaming process: {p_name} - maintaining original name."
        )
        new_n = p_name

    return new_n

def rename_natural_gas_processes(p_name, location):
    """
    This method renames natural gas extraction, processing, and transport 
    processes to follow the ILCD naming convention. The main elements in the final 
    name are:
        * delivery region - the location of the process - retrieved from the process 
                location attribute
    Final name format:
        Natural gas; Extraction, processing, and transport; {delivery region}
        Example:
            Natural gas; Extraction, processing, and transport; Midwest
    
    Parameters:
        p_name (str): The original process name.
        location (str): The location of the process, used to determine the 
            delivery region.
    Returns:
        str: The renamed process name in ILCD format.
    """
    if location == "United States of America (the)":
        location = "United States"

    try:
        new_n = (
            f"Natural gas extraction, processing, and transport; {location}"
        )
    except Exception as e:
        logging.error(
            f"Error renaming process: {p_name} - maintaining original name."
        )
        new_n = p_name
    return new_n