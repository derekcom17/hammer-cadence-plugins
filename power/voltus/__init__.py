#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Voltus.
#
#  See LICENSE for licence details.

import shutil
from typing import List, Dict, Optional, Callable, Tuple, Set, Any, cast
from itertools import product

import os
import errno
import json

from hammer_config import HammerJSONEncoder
from hammer_utils import get_or_else, optional_map, coerce_to_grid, check_on_grid, lcm_grid, in_place_unique
from hammer_vlsi import HammerPowerTool, HammerToolStep, MMMCCorner, MMMCCornerType, TimeValue, VoltageValue
from hammer_logging import HammerVLSILogging
import hammer_tech
from specialcells import CellType

import sys
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)),"../../common"))
from tool import CadenceTool


class Voltus(HammerPowerTool, CadenceTool):
    @property
    def post_synth_sdc(self) -> Optional[str]:
        # No post-synth SDC input for power...
        return None

    def tool_config_prefix(self) -> str:
        return "power.voltus"

    @property
    def env_vars(self) -> Dict[str, str]:
        new_dict = dict(super().env_vars)
        new_dict["VOLTUS_BIN"] = self.get_setting("power.voltus.voltus_bin")
        return new_dict

    @property
    def extra_corners_only(self) -> bool:
        return self.get_setting("power.inputs.extra_corners_only")

    @property
    def tech_stdcell_pgv_tcl(self) -> str:
        return os.path.join(self.run_dir, "tech_stdcell_pgv.tcl")

    @property
    def macro_pgv_tcl(self) -> str:
        return os.path.join(self.run_dir, "macro_pgv.tcl")

    @property
    def tech_lib_dir(self) -> str:
        return os.path.join(self.technology.cache_dir, "tech_pgv")

    @property
    def stdcell_lib_dir(self) -> str:
        return os.path.join(self.technology.cache_dir, "stdcell_pgv")

    @property
    def macro_lib_dir(self) -> str:
        return os.path.join(self.technology.cache_dir, "macro_pgv")

    @property
    def ran_tech_stdcell_pgv(self) -> bool:
        """ init_technology sets this to True if tech/stdcell PG views were generated by Hammer or provided by PDK """
        return self.attr_getter("_ran_tech_stdcell_pgv", False)

    @ran_tech_stdcell_pgv.setter
    def ran_tech_stdcell_pgv(self, val: bool) -> None:
        self.attr_setter("_ran_tech_stdcell_pgv", val)

    @property
    def gen_tech_stdcell_pgv(self) -> bool:
        """ init_technology sets this to True if stdcell PG views need to be generated """
        return self.attr_getter("_gen_tech_stdcell_pgv", False)

    @gen_tech_stdcell_pgv.setter
    def gen_tech_stdcell_pgv(self, val: bool) -> None:
        self.attr_setter("_gen_tech_stdcell_pgv", val)

    @property
    def ran_macro_pgv(self) -> bool:
        """ init_technology sets this to True if macro PG views were generated by Hammer """
        return self.attr_getter("_ran_macro_pgv", False)

    @ran_macro_pgv.setter
    def ran_macro_pgv(self, val: bool) -> None:
        self.attr_setter("_ran_macro_pgv", val)

    @property
    def gen_macro_pgv(self) -> bool:
        """ init_technology sets this to True if macro PG views need to be generated """
        return self.attr_getter("_gen_macro_pgv", False)

    @gen_macro_pgv.setter
    def gen_macro_pgv(self, val: bool) -> None:
        self.attr_setter("_gen_macro_pgv", val)

    @property
    def macro_pgv_cells(self) -> List[str]:
        """ init_technology populates the list of macros to generate PG views for """
        return self.attr_getter("_macro_pgv_cells", [])

    @macro_pgv_cells.setter
    def macro_pgv_cells(self, val: List[str]) -> None:
        self.attr_setter("_macro_pgv_cells", val)

    def tech_lib_filter(self) -> List[Callable[[hammer_tech.Library], bool]]:
        """ Filter only libraries from tech plugin """
        return [self.filter_for_tech_libs]

    def filter_for_tech_libs(self, lib: hammer_tech.Library) -> bool:
        return lib in self.technology.tech_defined_libraries

    def extra_lib_filter(self) -> List[Callable[[hammer_tech.Library], bool]]:
        """ Filter only libraries from vlsi.inputs.extra_libraries """
        return [self.filter_for_extra_libs]

    def filter_for_extra_libs(self, lib: hammer_tech.Library) -> bool:
        return lib in list(map(lambda el: el.store_into_library(), self.technology.get_extra_libraries()))

    def get_mmmc_pgv(self, corner: MMMCCorner) -> List[str]:
        return self.technology.read_libs([hammer_tech.filters.power_grid_library_filter],
                                             hammer_tech.HammerTechnologyUtils.to_plain_item,
                                             extra_pre_filters=[
                                                 self.filter_for_mmmc(voltage=corner.voltage, temp=corner.temp)])

    def get_mmmc_spice_models(self, corner: MMMCCorner) -> List[str]:
        return self.technology.read_libs([hammer_tech.filters.spice_model_file_filter],
                                             hammer_tech.HammerTechnologyUtils.to_plain_item,
                                             extra_pre_filters=[
                                                 self.filter_for_mmmc(voltage=corner.voltage, temp=corner.temp)])

    def get_mmmc_spice_corners(self, corner: MMMCCorner) -> List[str]:
        return self.technology.read_libs([hammer_tech.filters.spice_model_lib_corner_filter],
                                             hammer_tech.HammerTechnologyUtils.to_plain_item,
                                             extra_pre_filters=[
                                                 self.filter_for_mmmc(voltage=corner.voltage, temp=corner.temp)],
                                             must_exist=False)

    @property
    def steps(self) -> List[HammerToolStep]:
        return self.make_steps_from_methods([
            self.init_technology,
            self.init_design,
            self.static_power,
            self.active_power,
            self.static_rail,
            self.active_rail,
            self.run_voltus
        ])

    def init_technology(self) -> bool:
        corners = self.get_mmmc_corners()

	    # Options for set_pg_library_mode
        base_options = []  # type: List[str]
        if self.get_setting("power.voltus.lef_layer_map"):
            base_options.extend(["-lef_layer_map", self.get_setting("power.voltus.lef_layer_map")])

        # Setup commands for each PG library run
        base_cmds = ["set_db design_process_node {}".format(self.get_setting("vlsi.core.node"))]
        base_cmds.append("set_multi_cpu_usage -local_cpu {}".format(self.get_setting("vlsi.core.max_threads")))

        # First, check if tech plugin supplies power grid libraries
        tech_pg_libs = self.technology.read_libs([hammer_tech.filters.power_grid_library_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.tech_lib_filter())
        tech_lib_lefs = self.technology.read_libs([hammer_tech.filters.lef_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.tech_lib_filter())
        if len(tech_pg_libs) > 0:
            self.logger.info("Technology already provides PG libraries. Moving onto macro PG libraries.")
            self.ran_tech_stdcell_pgv = True
	    # Else, characterize tech & stdcell libraries only once
        elif not os.path.isdir(self.tech_lib_dir) or not os.path.isdir(self.stdcell_lib_dir):
            self.logger.info("Generating techonly and stdcell PG libraries for the first time...")
            ts_output = base_cmds.copy()
            # Get only the tech-defined libraries
            ts_output.append("read_physical -lef {{ {} }}".format(" ".join(tech_lib_lefs)))

            tech_options = base_options.copy()
            tech_options.extend(["-cell_type", "techonly"])
            # fillers, decaps
            stdfillers = self.technology.get_special_cell_by_type(CellType.StdFiller)
            decaps = self.technology.get_special_cell_by_type(CellType.Decap)
            tech_lib_sp = self.technology.read_libs([hammer_tech.filters.spice_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.tech_lib_filter())

            if not corners:
                # Start with tech-only library
                options = tech_options.copy()
                options.extend([
                    "-extraction_tech_file", self.get_qrc_tech(), # TODO: this assumes only 1 exists in no corners case
                    "-default_power_voltage", str(VoltageValue(self.get_setting("vlsi.inputs.supplies.VDD")).value_in_units("V"))
                ])
                ts_output.append("set_pg_library_mode {}".format(" ".join(options)))
                ts_output.append("write_pg_library -out_dir {}".format(self.tech_lib_dir))

                # Next do stdcell library
                options[options.index("techonly")] = "stdcells"
                if len(stdfillers) > 0:
                    stdfillers_names = list(map(lambda f: str(f), stdfillers[0].name))
                    options.extend(["-filler_cells", "{{ {} }} ".format(" ".join(stdfillers_names))])
                if len(decaps) > 0:
                    decaps_names = list(map(lambda d: str(d), decaps[0].name))
                    options.extend(["-decap_cells", "{{ {} }}".format(" ".join(decaps_names))])
                spice_models = self.technology.read_libs([hammer_tech.filters.spice_model_file_filter], hammer_tech.HammerTechnologyUtils.to_plain_item)
                spice_corners = self.technology.read_libs([hammer_tech.filters.spice_model_lib_corner_filter], hammer_tech.HammerTechnologyUtils.to_plain_item)
                if len(spice_models) == 0:
                    self.logger.error("Must specify Spice model files in tech plugin to generate stdcell PG libraries! Skipping.")
                    return True
                else:
                    options.extend(["-spice_models", " ".join(spice_models)])
                    if len(spice_corners) > 0:
                        options.extend(["-spice_corners", "{", "} {".join(spice_corners), "}"])
                if len(decaps) > 0 and len(tech_lib_sp) == 0:
                    self.logger.error("Must have Spice netlists in tech plugin for decap characterization in stdcell PG library! Skipping.")
                    return True
                else:
                    options.extend(["-spice_subckts", "{{ {} }}".format(" ".join(tech_lib_sp))])
                ts_output.append("set_pg_library_mode {}".format(" ".join(options)))
                ts_output.append("write_pg_library -out_dir {}".format(self.stdcell_lib_dir))

            else:
                for corner in corners:
                    # Start with tech-only library
                    options = tech_options.copy()
                    options.extend([
                        "-extraction_tech_file", self.get_mmmc_qrc(corner), #TODO: QRC should be tied to stackup
                        "-default_power_voltage", str(corner.voltage.value),
                        "-temperature", str(corner.temp.value)
                    ])
                    ts_output.append("set_pg_library_mode {}".format(" ".join(options)))
                    ts_output.append("write_pg_library -out_dir {}".format(os.path.join(self.tech_lib_dir, corner.name)))

                    # Next do stdcell library
                    options[options.index("techonly")] = "stdcells"
                    if len(stdfillers) > 0:
                        stdfillers_names = list(map(lambda f: str(f), stdfillers[0].name))
                        options.extend(["-filler_cells", "{{ {} }} ".format(" ".join(stdfillers_names))])
                    if len(decaps) > 0:
                        decaps_names = list(map(lambda d: str(d), decaps[0].name))
                        options.extend(["-decap_cells", "{{ {} }}".format(" ".join(decaps_names))])
                    spice_models = self.get_mmmc_spice_models(corner)
                    spice_corners = self.get_mmmc_spice_corners(corner)
                    if len(spice_models) == 0:
                        self.logger.error("Must specify Spice model files in tech plugin to generate stdcell PG libraries! Skipping.")
                        return True
                    else:
                        options.extend(["-spice_models", " ".join(spice_models)])
                        if len(spice_corners) > 0:
                            options.extend(["-spice_corners", "{", "} {".join(spice_corners), "}"])
                    if len(decaps) > 0 and len(tech_lib_sp) == 0:
                        self.logger.error("Must have Spice netlists in tech plugin for decap characterization in stdcell PG library! Skipping.")
                        return True
                    else:
                        options.extend(["-spice_subckts", "{{ {} }}".format(" ".join(tech_lib_sp))])
                    ts_output.append("set_pg_library_mode {}".format(" ".join(options)))
                    ts_output.append("write_pg_library -out_dir {}".format(os.path.join(self.stdcell_lib_dir, corner.name)))

            ts_output.append("exit")
            with open(self.tech_stdcell_pgv_tcl, "w") as f:
                f.write("\n".join(ts_output))
            self.gen_tech_stdcell_pgv = True
            self.ran_tech_stdcell_pgv = True
        else:
            self.logger.info("techonly and stdcell PG libraries already generated, skipping...")
            self.ran_tech_stdcell_pgv = True

        if self.get_setting("power.voltus.macro_pgv"):
            m_output = base_cmds.copy()
    	    # Characterize macro libraries once, unless list of extra libraries has been modified/changed
            tech_lef = tech_lib_lefs[0]
            extra_lib_lefs = self.technology.read_libs([hammer_tech.filters.lef_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.extra_lib_filter())
            extra_lib_mtimes = list(map(lambda l: os.path.getmtime(l), extra_lib_lefs))
            extra_lib_lefs_mtimes = dict(zip(extra_lib_lefs, extra_lib_mtimes))
            extra_lib_lefs_json = os.path.join(self.run_dir, "extra_lib_lefs.json")
            extra_pg_libs = self.technology.read_libs([hammer_tech.filters.power_grid_library_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.extra_lib_filter())
            # TODO: Use some filters w/ LEFUtils to extract cells from LEFs, e.g. MacroSize instead of using name field
            named_extra_libs = list(filter(lambda l: l.library.name is not None and l.library.power_grid_library not in extra_pg_libs, self.technology.get_extra_libraries()))  # type: List[hammer_tech.ExtraLibrary]

            if not os.path.isdir(self.macro_lib_dir):
                self.logger.info("Characterizing macros for the first time...")
                # First time: characterize all cells
                macros = list(map(lambda l: l.library.name, named_extra_libs))
                in_place_unique(macros)
                self.macro_pgv_cells = macros

                # Write dict of extra library LEFs
                with open(extra_lib_lefs_json, "w") as f:
                    f.write(json.dumps(extra_lib_lefs_mtimes, cls=HammerJSONEncoder, indent=4))
            else:
                # Figure out which cells to re-characterize
                prior_extra_lib_lefs = {}  # type: Dict[str, str]
                if os.path.exists(extra_lib_lefs_json):
                    with open(extra_lib_lefs_json, "r") as f:
                        prior_extra_lib_lefs = json.loads(f.read())
                # Write updated dict of extra library LEFs
                with open(extra_lib_lefs_json, "w") as f:
                    f.write(json.dumps(extra_lib_lefs_mtimes, cls=HammerJSONEncoder, indent=4))
                # Get LEFs which have been created/modified, match cell names if provided
                mod_lefs = dict(set(extra_lib_lefs_mtimes.items()) - set(prior_extra_lib_lefs.items())).keys()
                rechar_libs = list(filter(lambda l: l.library.lef_file in mod_lefs, named_extra_libs))
                macros = list(map(lambda l: l.library.name, rechar_libs))
                in_place_unique(macros)
                self.macro_pgv_cells = macros

            if len(self.macro_pgv_cells) > 0:
                self.logger.info("Characterizing the following macros: {}".format(" ".join(self.macro_pgv_cells)))
                # Write list of cells to characterize
                cells_list = os.path.join(self.run_dir, "macro_cells.txt")
                with open(cells_list, "w") as f:
                    f.write("\n".join(self.macro_pgv_cells))

                macro_options = base_options.copy()
                macro_options.extend([
                        "-cell_type", "macros",
                        "-cells_file", cells_list
                ])

                # File checks
                gds_map_file = self.get_gds_map_file()
                if gds_map_file is None:
                    self.logger.error("Must have GDS layer map for macro PG library generation! Skipping.")
                    return True
                else:
                    assert isinstance(gds_map_file, str)
                    macro_options.extend(["-stream_layer_map", gds_map_file])

                extra_lib_sp = self.technology.read_libs([hammer_tech.filters.spice_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.extra_lib_filter())
                if len(extra_lib_sp) == 0:
                    self.logger.error("Must have Spice netlists for macro PG library generation! Skipping.")
                    return True
                else:
                    macro_options.extend(["-spice_subckts", "{{ {} }}".format(" ".join(extra_lib_sp))])

                extra_lib_gds = self.technology.read_libs([hammer_tech.filters.gds_filter], hammer_tech.HammerTechnologyUtils.to_plain_item, self.extra_lib_filter())
                if len(extra_lib_gds) == 0:
                    self.logger.error("Must have GDS data for macro PG library generation! Skipping.")
                    return True
                else:
                    macro_options.extend(["-stream_files", "{{ {} }}".format(" ".join(extra_lib_gds))])

                m_output.append("read_physical -lef {{ {TECH_LEF} {EXTRA_LEFS} }}".format(TECH_LEF=tech_lef, EXTRA_LEFS=" ".join(extra_lib_lefs)))

                if not corners:
                    options = macro_options.copy()
                    options.extend([
                        "-extraction_tech_file", self.get_qrc_tech(), # TODO: this assumes only 1 exists in no corners case
                        "-default_power_voltage", str(VoltageValue(self.get_setting("vlsi.inputs.supplies.VDD")).value_in_units("V"))
                    ])
                    spice_models = self.technology.read_libs([hammer_tech.filters.spice_model_file_filter], hammer_tech.HammerTechnologyUtils.to_plain_item)
                    spice_corners = self.technology.read_libs([hammer_tech.filters.spice_model_lib_corner_filter], hammer_tech.HammerTechnologyUtils.to_plain_item)
                    if len(spice_models) == 0:
                        self.logger.error("Must specify Spice model files in tech plugin to generate macro PG libraries")
                        return True
                    else:
                        options.extend(["-spice_models", " ".join(spice_models)])
                        if len(spice_corners) > 0:
                            options.extend(["-spice_corners", "{", "} {".join(spice_corners), "}"])
                    m_output.append("set_pg_library_mode {}".format(" ".join(options)))
                    m_output.append("write_pg_library -out_dir {}".format(os.path.join(self.macro_lib_dir, corner.name)))

                else:
                    for corner in corners:
                        options = macro_options.copy()
                        options.extend([
                            "-extraction_tech_file", self.get_mmmc_qrc(corner), #TODO: QRC should be tied to stackup
                            "-default_power_voltage", str(corner.voltage.value),
                            "-temperature", str(corner.temp.value),
                        ])
                        spice_models = self.get_mmmc_spice_models(corner)
                        spice_corners = self.get_mmmc_spice_corners(corner)
                        if len(spice_models) == 0:
                            self.logger.error("Must specify Spice model files in tech plugin to generate macro PG libraries")
                            return True
                        else:
                            options.extend(["-spice_models", " ".join(spice_models)])
                            if len(spice_corners) > 0:
                                options.extend(["-spice_corners", "{", "} {".join(spice_corners), "}"])
                        m_output.append("set_pg_library_mode {}".format(" ".join(options)))
                        m_output.append("write_pg_library -out_dir {}".format(os.path.join(self.macro_lib_dir, corner.name)))

                m_output.append("exit")
                with open(self.macro_pgv_tcl, "w") as f:
                    f.write("\n".join(m_output))
                self.gen_macro_pgv = True
                self.ran_macro_pgv = True
            else:
                self.logger.info("macro PG libraries already generated and macros have not changed, skipping...")
                self.ran_macro_pgv = True
        else:
            self.logger.info("power.voltus.macro_pgv is False. Rail analysis will be incomplete over macro blocks.")

        return True

    def init_design(self) -> bool:
        verbose_append = self.verbose_append

        verbose_append("set_db design_process_node {}".format(self.get_setting("vlsi.core.node")))
        verbose_append("set_multi_cpu_usage -local_cpu {}".format(self.get_setting("vlsi.core.max_threads")))

        innovus_db = os.path.join(os.getcwd(), self.par_database)
        if innovus_db is None or not os.path.isdir(innovus_db):
            raise ValueError("Innovus database %s not found" % (innovus_db))

        verbose_append("read_db {}".format(innovus_db))

        verbose_append("check_pg_shorts -out_file shorts.rpt")

        # TODO (daniel) deal with multiple power domains
        for power_net in self.get_all_power_nets():
            vdd_net = power_net.name
        for gnd_net in self.get_all_ground_nets():
            vss_net = gnd_net.name
        verbose_append("set_power_pads -net {VDD} -format defpin".format(VDD=vdd_net))
        verbose_append("set_power_pads -net {VSS} -format defpin".format(VSS=vss_net))

        corners = self.get_mmmc_corners()
        if corners:
            setup_view_names = [] # type: List[str]
            hold_view_names = [] # type: List[str]
            extra_view_names = [] # type: List[str]
            rc_corners = [] # type: List[str]
            for corner in corners:
                # Setting up views for all defined corner types: setup, hold, extra
                if corner.type is MMMCCornerType.Setup:
                    corner_name = "{n}.{t}".format(n=corner.name, t="setup")
                    setup_view_names.append("{n}_view".format(n=corner_name))
                elif corner.type is MMMCCornerType.Hold:
                    corner_name = "{n}.{t}".format(n=corner.name, t="hold")
                    hold_view_names.append("{n}_view".format(n=corner_name))
                elif corner.type is MMMCCornerType.Extra:
                    corner_name = "{n}.{t}".format(n=corner.name, t="extra")
                    extra_view_names.append("{n}_view".format(n=corner_name))
                else:
                    raise ValueError("Unsupported MMMCCornerType")
                rc_corners.append("{n}_rc".format(n=corner_name))

            # Apply analysis views
            # TODO: should not need to analyze extra views as well. Defaulting to hold for now (min. runtime impact).
            verbose_append("set_analysis_view -setup {{ {setup_views} }} -hold {{ {hold_views} {extra_views} }}".format(
                setup_views=" ".join(setup_view_names),
                hold_views=" ".join(hold_view_names),
                extra_views=" ".join(extra_view_names)
            ))
            # Match spefs with corners. Ordering must match (ensured here by get_mmmc_corners())!
            for (spef, rc_corner) in zip(self.spefs, rc_corners):
                verbose_append("read_spef {spef} -rc_corner {corner}".format(spef=os.path.join(os.getcwd(), spef), corner=rc_corner))

        else:
            # TODO: remove hardcoded my_view string
            analysis_view_name = "my_view"
            verbose_append("set_analysis_view -setup {{ {setup_view} }} -hold {{ {hold_view} }}".format(
                setup_view=analysis_view_name,
                hold_view=analysis_view_name
            ))
            verbose_append("read_spef " + os.path.join(os.getcwd(), self.spefs[0]))

        return True

    def static_power(self) -> bool:
        verbose_append = self.verbose_append

        verbose_append("set_db power_method static")
        verbose_append("set_db power_write_static_currents true")
        verbose_append("set_db power_write_db true")

        # Report based on MMMC mode
        corners = self.get_mmmc_corners()
        if not corners:
            if self.extra_corners_only:
                self.logger.warning("power.inputs.extra_corners_only not valid in non-MMMC mode! Reporting static power for default analysis view only.")
            verbose_append("report_power -out_dir staticPowerReports")
        else:
            if self.extra_corners_only:
                extra_corners = list(filter(lambda c: c.type is MMMCCornerType.Extra, corners))
                if len(extra_corners) == 0:
                    self.logger.warning("power.inputs.extra_corners_only is true but no extra MMMC corners specified! Ignoring for static power.")
                else:
                    corners = extra_corners
            for corner in corners:
                # Setting up views for all defined corner types: setup, hold, extra
                if corner.type is MMMCCornerType.Setup:
                    view_name = "{c}.setup_view".format(c=corner.name)
                elif corner.type is MMMCCornerType.Hold:
                    view_name = "{c}.hold_view".format(c=corner.name)
                elif corner.type is MMMCCornerType.Extra:
                    view_name = "{c}.extra_view".format(c=corner.name)
                else:
                    raise ValueError("Unsupported MMMCCornerType")
                verbose_append("report_power -view {VIEW} -out_dir staticPowerReports.{VIEW}".format(VIEW=view_name))

        return True

    def active_power(self) -> bool:
        verbose_append = self.verbose_append

        # Active Vectorless Power Analysis
        verbose_append("set_db power_method dynamic_vectorless")
        # TODO (daniel) add the resolution as an option?
        verbose_append("set_dynamic_power_simulation -resolution 500ps")

        # Check MMMC mode
        corners = self.get_mmmc_corners()
        if not corners:
            if self.extra_corners_only:
                self.logger.warning("power.inputs.extra_corners_only not valid in non-MMMC mode! Reporting active power for default analysis view only.")
            verbose_append("report_power -out_dir activePowerReports")
        else:
            if self.extra_corners_only:
                extra_corners = list(filter(lambda c: c.type is MMMCCornerType.Extra, corners))
                if len(extra_corners) == 0:
                    self.logger.warning("power.inputs.extra_corners_only is true but no extra MMMC corners specified! Ignoring for active power.")
                else:
                    corners = extra_corners
            for corner in corners:
                # Setting up views for all defined corner types: setup, hold, extra
                if corner.type is MMMCCornerType.Setup:
                    view_name = "{c}.setup_view".format(c=corner.name)
                elif corner.type is MMMCCornerType.Hold:
                    view_name = "{c}.hold_view".format(c=corner.name)
                elif corner.type is MMMCCornerType.Extra:
                    view_name = "{c}.extra_view".format(c=corner.name)
                else:
                    raise ValueError("Unsupported MMMCCornerType")
                verbose_append("report_power -view {VIEW} -out_dir activePowerReports.{VIEW}".format(VIEW=view_name))

        # TODO (daniel) deal with different tb/dut hierarchies
        tb_name = self.get_setting("power.inputs.tb_name")
        tb_dut = self.get_setting("power.inputs.tb_dut")
        tb_scope = "{}/{}".format(tb_name, tb_dut)

        # TODO: These times should be either auto calculated/read from the inputs or moved into the same structure as a tuple
        start_times = self.get_setting("power.inputs.start_times")
        end_times = self.get_setting("power.inputs.end_times")

        # Active Vectorbased Power Analysis
        verbose_append("set_db power_method dynamic_vectorbased")
        for vcd_path, vcd_stime, vcd_etime in zip(self.waveforms, start_times, end_times):
            stime_ns = TimeValue(vcd_stime).value_in_units("ns")
            etime_ns = TimeValue(vcd_etime).value_in_units("ns")
            verbose_append("read_activity_file -reset -format VCD {VCD_PATH} -start {stime}ns -end {etime}ns -scope {TESTBENCH}".format(VCD_PATH=os.path.join(os.getcwd(), vcd_path), TESTBENCH=tb_scope, stime=stime_ns, etime=etime_ns))
            vcd_file = os.path.basename(vcd_path)
            # Report based on MMMC mode
            if not corners:
                verbose_append("report_power -out_dir activePower.{VCD_FILE}".format(VCD_FILE=vcd_file))
            else:
                for corner in corners:
                    # Setting up views for all defined corner types: setup, hold, extra
                    if corner.type is MMMCCornerType.Setup:
                        view_name = "{c}.setup_view".format(c=corner.name)
                    elif corner.type is MMMCCornerType.Hold:
                        view_name = "{c}.hold_view".format(c=corner.name)
                    elif corner.type is MMMCCornerType.Extra:
                        view_name = "{c}.extra_view".format(c=corner.name)
                    else:
                        raise ValueError("Unsupported MMMCCornerType")
                    verbose_append("report_power -view {VIEW} -out_dir activePowerReports.{VCD_FILE}.{VIEW}".format(VIEW=view_name, VCD_FILE=vcd_file))

            verbose_append("report_vector_profile -detailed_report true -out_file activePowerProfile.{VCD_FILE}".format(VCD_FILE=vcd_file))

        verbose_append("set_db power_method dynamic")
        for saif_path in self.saifs:
            verbose_append("set_dynamic_power_simulation -reset")
            verbose_append("read_activity_file -reset -format SAIF {SAIF_PATH} -scope {TESTBENCH}".format(SAIF_PATH=os.path.join(os.getcwd(), saif_path), TESTBENCH=tb_scope))
            saif_file=".".join(saif_path.split('/')[-2:])
            # Report based on MMMC mode
            if not corners:
                verbose_append("report_power -out_dir activePower.{SAIF_FILE}".format(SAIF_FILE=saif_file))
            else:
                for corner in corners:
                    # Setting up views for all defined corner types: setup, hold, extra
                    if corner.type is MMMCCornerType.Setup:
                        view_name = "{c}.setup_view".format(c=corner.name)
                    elif corner.type is MMMCCornerType.Hold:
                        view_name = "{c}.hold_view".format(c=corner.name)
                    elif corner.type is MMMCCornerType.Extra:
                        view_name = "{c}.extra_view".format(c=corner.name)
                    else:
                        raise ValueError("Unsupported MMMCCornerType")
                    verbose_append("report_power -view {VIEW} -out_dir activePowerReports.{SAIF_FILE}.{VIEW}".format(VIEW=view_name, SAIF_FILE=saif_file))

        return True

    def rail_analysis(self, method: str, power_dir: str, output_dir: Optional[str] = None) -> bool:
        """
        Generic method for rail analysis
        params:
        - method: "static" or "dynamic"
        - power_dir: relative path to static or active power current files
        - output_dir: relative path to rail analysis output dir
        """
        verbose_append = self.verbose_append

        if not output_dir:
            output_dir = method + "RailReports"

        # Decide accuracy based on existence of PGV libraries, unless overridden
        accuracy = self.get_setting("power.voltus.rail_accuracy")
        if not accuracy:
            accuracy = "hd" if self.ran_tech_stdcell_pgv else "xd" # hd still works w/o macro PG views

        base_options = [
            "-method", method,
            "-accuracy", accuracy,
            "-process_techgen_em_rules", "true",
            "-em_peak_analysis", "true",
            "-enable_rlrp_analysis", "true",
            "-gif_resolution", "high",
            "-verbosity", "true"
        ]
        if method == "static":
            base_options.extend(["-enable_sensitivity_analysis", "true"])

        # TODO: Need combinations of all power nets + voltage domains
        pg_nets = self.get_all_power_nets() + self.get_all_ground_nets()
        # Report based on MMMC corners
        corners = self.get_mmmc_corners()
        if not corners:
            if self.extra_corners_only:
                self.logger.warning("power.inputs.extra_corners_only not valid in non-MMMC mode! Reporting rail analysis for default analysis view only.")
            options = base_options.copy()
            pg_libs = self.technology.read_libs([hammer_tech.filters.power_grid_library_filter], hammer_tech.HammerTechnologyUtils.to_plain_item)
            if self.ran_tech_stdcell_pgv:
                pg_libs.append(os.path.join(self.tech_lib_dir, "techonly.cl"))
                pg_libs.append(os.path.join(self.stdcell_lib_dir, "stdcells.cl"))
            if self.ran_macro_pgv:
                pg_libs.extend(list(map(lambda l: os.path.join(self.macro_lib_dir, "macros_{}.cl".format(l)), self.macro_pgv_cells)))
            if len(pg_libs) == 0:
                self.logger.warning("No PG libraries are available! Rail analysis is skipped.")
                return True
            options.extend(["-power_grid_libraries", "{{ {} }}".format(" ".join(pg_libs))])
            verbose_append("set_rail_analysis_config {}".format(" ".join(options)))
            # TODO: get nets and .ptiavg files using TCL from the .ptifiles file in the power reports directory
            power_data = list(map(lambda n: "{POWER_DIR}/{METHOD}_{NET}.ptiavg".format(
                POWER_DIR=power_dir,
                METHOD=method,
                NET=n.name), pg_nets))
            verbose_append("set_power_data -format current {{ {} }}".format(" ".join(power_data)))
            verbose_append("report_rail -output_dir {} -type domain ALL".format(output_dir))
            # TODO: Find highest run number, increment by 1 to enable reporting IRdrop regions
        else:
            if self.extra_corners_only:
                extra_corners = list(filter(lambda c: c.type is MMMCCornerType.Extra, corners))
                if len(extra_corners) == 0:
                    self.logger.warning("power.inputs.extra_corners_only is true but no extra MMMC corners specified! Ignoring for rail analysis.")
                else:
                    corners = extra_corners
            for corner in corners:
                options = base_options.copy()
                if corner.type is MMMCCornerType.Setup:
                    view_name = corner.name + ".setup_view"
                elif corner.type is MMMCCornerType.Hold:
                    view_name = corner.name + ".hold_view"
                elif corner.type is MMMCCornerType.Extra:
                    view_name = corner.name + ".extra_view"
                else:
                    raise ValueError("Unsupported MMMCCornerType")
                pg_libs = self.get_mmmc_pgv(corner)
                if self.ran_tech_stdcell_pgv:
                    pg_libs.append(os.path.join(self.tech_lib_dir, corner.name, "techonly.cl"))
                    pg_libs.append(os.path.join(self.stdcell_lib_dir, corner.name, "stdcells.cl"))
                if self.ran_macro_pgv:
                    pg_libs.extend(list(map(lambda l: os.path.join(self.macro_lib_dir, corner.name, "macros_{}.cl".format(l)), self.macro_pgv_cells)))
                if len(pg_libs) == 0:
                    self.logger.warning("No PG libraries are available! Rail analysis is skipped.")
                    return True

                options.extend([
                    "-power_grid_libraries", "{{ {} }}".format(" ".join(pg_libs)),
                    "-analysis_view", view_name,
                    "-temperature", str(corner.temp.value)
                ])
                verbose_append("set_rail_analysis_config {}".format(" ".join(options)))
                verbose_append("set_power_data -reset")
                # TODO: get nets and .ptiavg files using TCL from the .ptifiles file in the power reports directory
                power_data = list(map(lambda n: "{POWER_DIR}.{VIEW}/{METHOD}_{NET}.ptiavg".format(
                    POWER_DIR=power_dir,
                    VIEW=view_name,
                    METHOD=method,
                    NET=n.name), pg_nets))
                verbose_append("set_power_data -format current {{ {} }}".format(" ".join(power_data)))
                verbose_append("report_rail -output_dir {} -type domain ALL".format(output_dir))
                # TODO: Find highest run number, increment by 1 to enable reporting IRdrop regions

        return True

    def static_rail(self) -> bool:
        return self.rail_analysis("static", "staticPowerReports")

    def active_rail(self) -> bool:
        # Vectorless database
        passed = self.rail_analysis("dynamic", "activePowerReports", "activeRailReports")

        # Vectorbased databases
        for vcd_path in self.waveforms:
            passed = self.rail_analysis("dynamic", "activePower." + os.path.basename(vcd_path), "activeRailReports." + os.path.basename(vcd_path))
        for saif_path in self.saifs:
            saif_file=".".join(saif_path.split('/')[-2:])
            passed = self.rail_analysis("dynamic", "activePower." + saif_file, "activeRailReports." + saif_file)
        return passed

    def run_voltus(self) -> bool:
        verbose_append = self.verbose_append

        """Close out the power script and run Voltus"""
        # Quit Voltus
        verbose_append("exit")

        # Create power analysis script
        power_tcl_filename = os.path.join(self.run_dir, "power.tcl")

        with open(power_tcl_filename, "w") as f:
            f.write("\n".join(self.output))

        # Build args
        base_args = [
            self.get_setting("power.voltus.voltus_bin"),
            "-no_gui",
            "-common_ui",
            "-init"
        ]

        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False

        # Run PG lib gen, if needed
        if self.gen_tech_stdcell_pgv:
            args = base_args.copy()
            args.append(self.tech_stdcell_pgv_tcl)
            self.run_executable(args, cwd=self.run_dir)
        if self.gen_macro_pgv:
            args = base_args.copy()
            args.append(self.macro_pgv_tcl)
            self.run_executable(args, cwd=self.run_dir)

        args = base_args.copy()
        args.append(power_tcl_filename)
        self.run_executable(args, cwd=self.run_dir)

        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True

        return True



tool = Voltus
