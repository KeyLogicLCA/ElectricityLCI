#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# egrid_flowbyfacilty.py
#
##############################################################################
# REQUIRED MODULES
##############################################################################
import stewi
from electricitylci.model_config import model_specs


##############################################################################
# FUNCTIONS
##############################################################################
# Get inventory data to get net generation per facility
egrid_flowbyfacility = stewi.getInventory("eGRID", model_specs.egrid_year)

# Peek at it
egrid_flowbyfacility.head(50)
