"""
Module that contains fonction for adcp data quality control.

Based on the adcp2nc package by jeanlucshaw: https://github.com/jeanlucshaw/adcp2nc/

Notes:
------
   Velocity in any direction are set to NaN if greater than 15 meter per seconds.
   Temperature has a fixed thresholds for flags [-2, 32] Celcius value outside
   have Flag == 4. Pressure has a fixed thresholds for flags [0, 180] dbar value
   outside have Flag == 4. Failing amplitude, correlation and percentgood, roll,
   pitch, horizontal velocity or vertical velocity test returns Flags == 3.
   Value below sidelobe depth returns Flags == 4.


   The same threshold for amplitude, correlation and percentgood are applied to
   sentinelV fifth beam data.

   Tests returns True when failed.

   SeaDataNet Quality Control Flags Value
   * 0: no_quality_control
   * 1: good_value
   * 2: probably_good_value
   * 3: probably_bad_value
       - Unusual data value, inconsistent with real phenomena.
   * 4: bad_value
       - Obviously erroneous data value.
   * 5: changed_value
   * 6: value_below_detection
   * 7: value_in_excess
   * 8: interpolated_value
   * 9: missing_value

NOTE
IML flags meaning : (Basicaly the same)
   * 0: no_quality_control
   * 1: value_seems_correct
   * 2: value_appears_inconsistent_with_other_values
   * 3: values_seems_doubtfull
   * 4: value_seems_erroneuous
   * 5: value_was_modified
   * 9: value_missing

"""
import typing as tp
from pathlib import Path

import numpy as np
import xarray as xr
from magtogoek.tools import circular_distance
from magtogoek.utils import Logger
from pandas import Timestamp
from scipy.stats import circmean

# Brand dependent quality control defaults
#    rti_qc_defaults = dict(amp_th=20)
#    rdi_qc_defaults = dict(amp_th=0)


IMPLAUSIBLE_VEL_TRESHOLD = 15  # meter per second
MIN_TEMPERATURE = -2  # Celcius
MAX_TEMPERATURE = 32  # Celcius
MIN_PRESSURE = 0  # dbar
MAX_PRESSURE = 10000  # dbar (mariana trench pressure)

l = Logger(level=0)
FLAG_REFERENCE = "BODC SeaDataNet"
FLAG_VALUES = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)
FLAG_MEANINGS = (
    "no_quality_control",
    "good_value",
    "probably_good_value",
    "probably_bad_value",
    "bad_value",
    "changed_value",
    "value_below_detection",
    "value_in_excess",
    "interpolated_value",
    "missing_value",
)


def no_adcp_quality_control(dataset):
    """Adds var_QC ancillary variables to dataset with value 0.

    ANcillary variables:  temperature, pres, u, v, w.

    SeaDataNet Quality Control Flags Value
    * 0: no_quality_control

    Parameters
    ----------
    dataset :
        ADCP dataset formatted as done by adcp_init.
    """
    l.reset()
    l.section("No Quality Controlled")

    l.log("No quality control carried out")
    variables = ["temperature", "pres", "u", "v", "w"]
    for var in variables:
        if var in dataset:
            dataset[var + "_QC"] = dataset[var].copy().astype("int8") * 0

    dataset.attrs["flags_reference"] = FLAG_REFERENCE
    dataset.attrs["flags_values"] = FLAG_VALUES
    dataset.attrs["flags_meanings"] = FLAG_MEANINGS
    dataset.attrs["quality_comments"] = "No quality control."


def adcp_quality_control(
    dataset: tp.Type[xr.Dataset],
    amp_th: float = 30,
    corr_th: float = 64,
    pg_th: float = 90,
    roll_th: float = 20,
    pitch_th: float = 20,
    horizontal_vel_th: float = 5,
    vertical_vel_th: float = 5,
    error_vel_th: float = 5,
    motion_correction_mode: float = None,
    sidelobes_correction: bool = False,
    bottom_depth: float = None,
) -> tp.Type[xr.Dataset]:
    """
    Perform ADCP quality control.

    This was adaptated from jeanlucshaw adcp2nc package.

    Parameters
    ----------
    dataset :
        ADCP dataset formatted as done by adcp_init.
    amp_th :
        Require more than this amplitude values.
    pg_th :
        Require more than this percentage of good 4-beam transformations.
    corr_th :
        Require more than this beam correlation value.
    roll_th :
        Require roll values be smaller than this value (degrees).
    pitch_th :
        Require pitch values be smaller than this value (degrees).
    horizontal_vel_th:
        Require u, v  values be smaller than this value (meter per seconds).
    veritcal_vel_th:
        Require w values be smaller than this value (meter per seconds).
    error_vel_th:
        Require e values be smaller than this value (meter per seconds).
    motion_correction
        If 'nav' or 'bt' will corrected velocities from the platform motion,
        will either correct u,v,w with navigation ('nav') or bottom track ('bt')
        data.
    sidelobes_correction :
        Use fixed depth or bottom track range to remove side lobe
        contamination. Set to either "dep" or "bt" or None.
    bottom_depth :
        If not `None`, this depth used for removing side lobe contamination.
    beam_angle :
        Force a beam angle configuration and overwrite the value in dataset.
    xducer_depth :
        Force a depth for the adcp and overwrite the value in dataset.

    Notes:
    ------
       Velocity in any direction are set to NaN if greater than 15 meter per seconds.
       Temperature has a fixed thresholds for flags [-2, 32] Celcius value outside
       have Flag == 4. Pressure has a fixed thresholds for flags [0, 180] dbar value
       outside have Flag == 4. Failing amplitude, correlation and percentgood, roll,
       pitch, horizontal velocity or vertical velocity test returns Flags == 3.
       Value below sidelobe depth returns Flags == 4.


       The same threshold for amplitude, correlation and percentgood are applied to
       sentinelV fifth beam data.

       Tests returns True when failed.

       SeaDataNet Quality Control Flags Value
       * 0: no_quality_control
       * 1: good_value
       * 2: probably_good_value
       * 3: probably_bad_value
           - Unusual data value, inconsistent with real phenomena.
       * 4: bad_value
           - Obviously erroneous data value.
       * 5: changed_value
       * 6: value_below_detection
       * 7: value_in_excess
       * 8: interpolated_value
       * 9: missing_value
    """
    l.reset()
    l.section("Quality Control")

    if motion_correction_mode:
        motion_correction(dataset, motion_correction_mode)

    set_implausible_vel_to_nan(dataset, thres=IMPLAUSIBLE_VEL_TRESHOLD)

    vel_flags = np.ones(dataset.depth.shape + dataset.time.shape).astype(int)

    vel_qc_test = []

    if amp_th:
        l.log(f"amplitude threshold {amp_th}")
        amp_flag = amplitude_test(dataset, amp_th)
        vel_flags[amp_flag] = 3
        vel_qc_test.append(f"amplitude_threshold:{amp_th}")

    if corr_th:
        l.log(f"correlation threshold {corr_th}")
        corr_flag = correlation_test(dataset, corr_th)
        vel_flags[corr_flag] = 3
        vel_qc_test.append(f"correlation_threshold:{corr_th}")

    if pg_th:
        l.log(f"percentgood threshold {pg_th}")
        pg_flag = percentgood_test(dataset, pg_th)
        vel_flags[pg_flag] = 3
        vel_qc_test.append(f"percentgood_threshold:{pg_th}")

    if horizontal_vel_th:
        l.log(f"horizontal velocity threshold {horizontal_vel_th} m/s")
        horizontal_vel_flag = horizontal_vel_test(dataset, horizontal_vel_th)
        vel_flags[horizontal_vel_flag] = 3
        vel_qc_test.append(f"horizontal_velocity_threshold:{horizontal_vel_th} m/s")

    if vertical_vel_th:
        l.log(f"vertical velocity threshold {vertical_vel_th} m/s")
        vertical_vel_flag = vertical_vel_test(dataset, vertical_vel_th)
        vel_flags[vertical_vel_flag] = 3
        vel_qc_test.append(f"vertical_velocity_threshold:{vertical_vel_th} m/s")

    if error_vel_th:
        l.log(f"error velocity threshold {error_vel_th} m/s")
        error_vel_flag = error_vel_test(dataset, error_vel_th)
        vel_flags[error_vel_flag] = 3
        vel_qc_test.append(f"velocity_error_threshold:{error_vel_th} m/s")

    if roll_th:
        l.log(f"roll threshold {roll_th} degree")
        roll_flag = np.tile(roll_test(dataset, roll_th), dataset.depth.shape + (1,))
        vel_flags[roll_flag] = 3
        vel_qc_test.append(f"roll_threshold:{roll_th} degree")

    if pitch_th:
        l.log(f"pitch threshold {pitch_th} degree")
        pitch_flag = np.tile(pitch_test(dataset, pitch_th), dataset.depth.shape + (1,))
        vel_flags[pitch_flag] = 3
        vel_qc_test.append(f"pitch_threshold:{pitch_th} degree")

    if sidelobes_correction:
        sidelobe_flag = sidelobe_test(dataset, bottom_depth)
        if sidelobe_flag:
            l.log(f"Sidelobe correction carried out.")
            vel_flags[sidelobe_flag] = 4
            vel_qc_test.append("sidelobes")

    if "pres" in dataset:
        l.log(f"Good pressure range {MIN_PRESSURE} to {MAX_PRESSURE} dbar")
        pressure_QC = np.ones(dataset.pres.shape)
        pressure_flags = pressure_test(dataset)
        pressure_QC[pressure_flags] = 4
        dataset["pres_QC"] = (["time"], pressure_QC)
        vel_flags[np.tile(pressure_flags, dataset.depth.shape + (1,))] = 4
        dataset["pres_QC"].attrs[
            "quality_test"
        ] = f"presssure_threshold: less than {MIN_PRESSURE} dbar and greater than {MAX_PRESSURE} dbar"
        vel_qc_test.append(dataset["pres_QC"].attrs["quality_test"])

    if "temperature" in dataset:
        l.log(f"Good temperature range {MIN_TEMPERATURE} to {MAX_TEMPERATURE} celsius")
        temperature_QC = np.ones(dataset.temperature.shape)
        temperature_QC[temperature_test(dataset)] = 4
        dataset["temperature_QC"] = (["time"], temperature_QC)
        dataset["temperature_QC"].attrs[
            "quality_test"
        ] = f"temperature_threshold: less than {MIN_TEMPERATURE} celcius and greater than {MAX_TEMPERATURE} celsius"
    if "vb_vel" in dataset:
        l.log(
            "Fifth beam quality control carried out with"
            + f"amplitude_threshold: {amp_th}" * ("vb_amp" in dataset)
            + f", correlation_threshold: {corr_th}" * ("vb_corr" in dataset)
            + f", percentgood_threshold: {pg_th}" * ("vb_pg" in dataset)
            + "."
        )
        vb_flag = vertical_beam_test(dataset, amp_th, corr_th, pg_th)
        dataset["vb_vel_QC"] = (["depth", "time"], vb_flag * 3)
        dataset["vb_vel_QC"].attrs["quality_test"] = (
            f"amplitude_threshold: {amp_th}\n" * ("vb_amp" in dataset)
            + f"correlation_threshold: {corr_th}\n" * ("vb_corr" in dataset)
            + f"percentgood_threshold: {pg_th}\n" * ("vb_pg" in dataset)
        )

    missing_vel = np.bitwise_or(
        *(~np.isfinite(dataset[v]).data for v in ("u", "v", "w"))
    )
    vel_flags[missing_vel] = 9

    for v in ("u", "v", "w"):
        dataset[v + "_QC"] = (["depth", "time"], vel_flags)
        dataset[v + "_QC"].attrs["quality_test"] = "\n".join(vel_qc_test)

    for var in list(dataset.variables):
        if "_QC" in var:
            dataset[var].attrs["quality_date"] = Timestamp.now().strftime("%Y-%m-%d")
            dataset[var].attrs["flag_meanings"] = FLAG_MEANINGS
            dataset[var].attrs["flag_values"] = FLAG_VALUES
            dataset[var].attrs["flag_reference"] = FLAG_REFERENCE

    dataset.attrs["quality_comments"] = l.logbook
    l.log(f"Quality Control was carried out with {l.w_count} warnings")
    dataset.attrs["logbook"] += l.logbook

    dataset.attrs["flags_reference"] = FLAG_REFERENCE
    dataset.attrs["flags_values"] = FLAG_VALUES
    dataset.attrs["flags_meanings"] = FLAG_MEANINGS


def motion_correction(dataset: tp.Type[xr.Dataset], mode: str):
    """Carry motion correction on velocities.

    If mode is 'bt' the motion correction is along x, y, z.
    If mode is 'nav' the motion correction is along x, y.
    """
    if mode == "bt":
        if all(f"bt_{v}" in dataset for v in ["u", "v", "w"]):
            for field in ["u", "v", "w"]:
                dataset[field] -= dataset[f"bt_{field}"].values
            l.log("Motion correction carried out with bottom track")
        else:
            l.warning(
                "Motion correction aborded. Bottom velocity (bt_u, bt_v, bt_w) missing"
            )
    elif mode == "nav":
        if all(f"{v}_ship" in dataset for v in ["u", "v"]):
            for field in ["u", "v"]:
                dataset[field] += np.tile(
                    dataset[field + "_ship"].where(np.isfinite(dataset.lon.values), 0),
                    (dataset.depth.size, 1),
                )
                dataset[f"{field}ship"].values = dataset[field].values
                l.log("Motion correction carried out with navigation")
        else:
            l.warning(
                "Motion correction aborded. Navigation velocity (u_ship, v_ship) missing"
            )
    else:
        l.warning(
            "Motion correction aborded. Motion correction mode invalid. ('bt' or 'nav')"
        )


def set_implausible_vel_to_nan(dataset: tp.Type[xr.Dataset], thres: float = 15):
    """Set bin with improbable values to Nan."""
    for v in ["u", "v", "w"]:
        plausible = (dataset[v] > -thres) & (dataset[v] < thres)
        dataset["u"] = dataset["u"].where(plausible)
        dataset["v"] = dataset["v"].where(plausible)
        dataset["w"] = dataset["w"].where(plausible)
        # flag 4: bad values ? now missing ?


def correlation_test(dataset, threshold):
    """FIXME
    NOTE JeanLucShaw used absolute but is it needed ?"""

    if all(f"corr{i}" in dataset for i in range(1, 5)):
        return (
            (dataset.corr1 < threshold)
            & (dataset.corr2 < threshold)
            & (dataset.corr3 < threshold)
            & (dataset.corr4 < threshold)
        ).data
    else:
        l.warnings("Correlation test aborted. Missing one or more corr data")
        return np.full(dataset.depth.shape + dataset.time.shape, False)


def amplitude_test(dataset, threshold):
    """FIXME
    NOTE JeanLucShaw used absolute but is it needed ?"""
    if all(f"amp{i}" in dataset for i in range(1, 5)):
        return (
            (dataset.amp1 < threshold)
            & (dataset.amp2 < threshold)
            & (dataset.amp3 < threshold)
            & (dataset.amp4 < threshold)
        ).data
    else:
        l.warnings("Amplitude test aborted. Missing one or more corr data")
        return np.full(dataset.depth.shape + dataset.time.shape, False)


def percentgood_test(dataset, threshold):
    """FIXME
    NOTE JeanLucShaw used absolute but is it needed ?"""
    if "pg" in dataset:
        return (dataset.pg < threshold).data
    else:
        l.warnings("Percent Good test aborted. Missing one or more corr data")
        return np.full(dataset.depth.shape + dataset.time.shape, False)


def roll_test(dataset: tp.Type[xr.Dataset], thres: float) -> tp.Type[np.array]:
    """FIXME
    Roll conditions (True fails)
    Distance from mean"""
    if "roll_" in dataset:
        roll_mean = circmean(dataset.roll_.values, low=-180, high=180)
        roll_from_mean = circular_distance(dataset.roll_.values, roll_mean, units="deg")
        return roll_from_mean > thres
    else:
        l.warnings("Roll test aborted. Missing one or more corr data")
        return np.full(dataset.depth.shape + dataset.time.shape, False)


def pitch_test(dataset: tp.Type[xr.Dataset], thres: float) -> tp.Type[np.array]:
    """FIXME
    Pitch conditions (True fails)
    Distance from Mean
    """
    if "pitch" in dataset:
        pitch_mean = circmean(dataset.pitch.values, low=-180, high=180)
        pitch_from_mean = circular_distance(
            dataset.pitch.values, pitch_mean, units="deg"
        )
        return pitch_from_mean > thres

    else:
        l.warnings("Pitch test aborted. Missing one or more corr data")
        return np.full(dataset.depth.shape + dataset.time.shape, False)


def horizontal_vel_test(
    dataset: tp.Type[xr.Dataset], thres: float
) -> tp.Type[np.array]:
    """FIXME
    None finite value value will also fail"""

    horizontal_velocity = np.sqrt(dataset.u ** 2 + dataset.v ** 2)

    return np.greater(
        horizontal_velocity.values,
        thres,
        where=np.isfinite(horizontal_velocity),
    )


def vertical_vel_test(dataset: tp.Type[xr.Dataset], thres: float) -> tp.Type[np.array]:
    """FIXME
    None finite value value will also fail"""
    return np.greater(
        abs(dataset.w.values),
        thres,
        where=np.isfinite(dataset.w.values),
    )


def error_vel_test(dataset: tp.Type[xr.Dataset], thres: float) -> tp.Type[np.array]:
    """FIXME
    None finite value value will also fail"""
    return np.greater(
        abs(dataset.e.values),
        thres,
        where=np.isfinite(dataset.w.values),
    )


def vertical_beam_test(
    dataset: tp.Type[xr.Dataset], amp_thres: float, corr_thres: float, pg_thres: float
) -> tp.Type[np.array]:
    """FIXME"""
    vb_test = np.full(dataset.depth.shape + dataset.time.shape, False)
    if "vb_amp" in dataset.variables and amp_thres:
        vb_test[dataset.vb_amp < amp_thres] = True
    if "vb_corr" in dataset.variables and corr_thres:
        vb_test[dataset.vb_corr < corr_thres] = True
    if "vb_pg" in dataset.variables and pg_thres:
        vb_test[dataset.vb_pg < pg_thres] = True

    return vb_test


def sidelobe_test(dataset: tp.Type[xr.Dataset], bottom_depth: float = None):
    """FIXME
    Test for sidelobe contamination (True fails).

    Returns a boolean array or a False statement if the test cannot be carried out.

    Equation :
        Downward: max_depth = XducerDepth + (bottom_depth + XducerDepth) * cos(beam_angle)
        Unward: min_depth = XducerDepth * ( 1 - cos(beam_angle))

    Parameters
    ----------
    depth : optional
        Fixed bottom depth to use for sidelobe correction
    """

    if dataset.attrs["beam_angle"] and dataset.attrs["orientation"]:
        cos_angle = np.cos(np.radians(dataset.attrs["beam_angle"]))
        depth_array = np.tile(dataset.depth.data, dataset.time.shape + (1,))

        if "xducer_depth" in dataset and "xducer_depth" not in dataset.attrs:
            xducer_depth = dataset["xducer_depth"].data
        elif "xducer_depth" in dataset.attrs:
            xducer_depth = np.tile(dataset.attrs["xducer_depth"], dataset.time.shape)
        else:
            l.warning(
                "Sidelobes correction aborded. Adcp depth `xducer_depth` not provided."
            )
            return False

        if dataset.attrs["orientation"] == "down":
            if bottom_depth:
                bottom_depth = bottom_depth
            elif "bt_depth" in dataset:
                bottom_depth = dataset.bt_depth.data
            else:
                l.warning(
                    "Sidelobes correction aborded. Bottom depth not found or provided."
                )
                return False

            max_depth = xducer_depth + (bottom_depth - xducer_depth) * cos_angle
            return depth_array.T > max_depth

        elif dataset.attrs["orientation"] == "up":

            min_depth = xducer_depth * (1 - cos_angle)

            return depth_array.T < min_depth.T

        else:
            l.warning(
                "Can not correct for sidelobes, `adcp_orientation` parameter not set."
            )
            return False


def temperature_test(dataset):
    """FIXME"""
    return np.bitwise_or(
        dataset.temperature > MAX_TEMPERATURE, dataset.temperature < MIN_TEMPERATURE
    ).data


def pressure_test(dataset):
    """FIXME"""
    return np.bitwise_or(dataset.pres > MAX_PRESSURE, dataset.pres < MIN_PRESSURE).data


if __name__ == "__main__":
    import matplotlib.pyplot as plt
    from magtogoek.adcp.loader import load_adcp_binary

    sillex_path = "/media/jeromejguay/5df6ae8c-2af4-4e5b-a1e0-a560a316bde3/home/jeromejguay/WorkSpace_2019/Data/Raw/ADCP/"
    sillex_fns = ["COR1805-ADCP-150kHz009_000001", "COR1805-ADCP-150kHz009_000002"]

    v50_files = (
        "/media/jeromejguay/Bruno/TREX2020/V50/TREX2020_V50_20200911T121242_003_*.ENX"
    )

    pd0_sw_path = "/home/jeromejguay/ImlSpace/Projects/magtogoek/test/files/sw_300_4beam_20deg_piston.pd0"

    ens_sw_path = (
        "/home/jeromejguay/ImlSpace/Projects/magtogoek/test/files/rowetech_seawatch.ens"
    )

    test = "SW_PD0"

    if test == "SV":
        ds = load_adcp_binary(
            v50_files,
            sonar="sv",
            yearbase=2020,
            orientation="down",
        )

    if test == "ENX":
        ds = load_adcp_binary(
            #            [sillex_path + fn + ".ENX" for fn in sillex_fns],
            sillex_path + "COR1805-ADCP-150kHz009.ENX",
            sonar="os",
            yearbase=2018,
            orientation="down",
        )
    if test == "SW_PD0":
        ds = load_adcp_binary(
            pd0_sw_path, sonar="sw_pd0", yearbase=2020, orientation="down"
        )
    if test == "ENS":
        ds = load_adcp_binary(
            ens_sw_path,
            sonar="sw",
            yearbase=2020,
            orientation="down",
            leading_index=None,
            trailing_index=None,
        )

    adcp_quality_control(
        ds,
        sidelobes_correction=True,
        bottom_depth=None,
        motion_correction_mode="bt",
        roll_th=20,
        pitch_th=20,
        horizontal_vel_th=2,
        vertical_vel_th=0.1,
    )

    ds.u.where(ds.u_QC == 1).plot()

    plt.show()
