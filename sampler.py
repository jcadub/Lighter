import math
import time
from array import array


def sample_breakers(adc, breakers, voltage_cal, interval_s=1.0):
    """
    Sample all breakers for one interval and return power readings.

    Cycles through all breakers in round-robin, reading a voltage+current
    pair per breaker per loop, until the interval elapses. This mirrors
    the Java Sampler which interleaves breaker reads within each AC cycle.

    Args:
        adc:        ADCReader instance
        breakers:   list of dicts, each with keys:
                      port, panel, space, port_cal, breaker_cal,
                      low_pass_filter, polarity, double_power
        voltage_cal: hub voltage calibration factor (hub.voltageCalibrationFactor)
        interval_s: sampling window in seconds (default 1.0)

    Returns:
        list of dicts: panel, space, power (watts), voltage (Vrms), samples
    """
    n = len(breakers)
    if n == 0:
        return []

    # Pre-allocate sample buffers as unsigned short arrays (0-1023 fits in uint16)
    # 20000 is a safe upper bound; Pi Zero W will get far fewer in Python
    MAX_SAMPLES = 20000
    v_buf = [array('H', bytes(MAX_SAMPLES * 2)) for _ in range(n)]
    i_buf = [array('H', bytes(MAX_SAMPLES * 2)) for _ in range(n)]
    counts = [0] * n

    end_time = time.monotonic() + interval_s

    # Round-robin sampling: read voltage+current for each breaker in turn
    while time.monotonic() < end_time:
        for idx in range(n):
            s = counts[idx]
            if s < MAX_SAMPLES:
                # Read voltage and current as close together as possible
                v_buf[idx][s] = adc.read_voltage()
                i_buf[idx][s] = adc.read_current(breakers[idx]['port'])
                counts[idx] += 1

    results = []
    for idx in range(n):
        cnt = counts[idx]
        if cnt == 0:
            continue

        b = breakers[idx]

        # Calculate DC offsets (mean of raw ADC values)
        v_offset = sum(v_buf[idx][:cnt]) / cnt
        i_offset = sum(i_buf[idx][:cnt]) / cnt

        p_sum = 0.0
        v_rms_sum = 0.0
        low_samples = 0
        low_pass = b.get('low_pass_filter', 0.0)

        for s in range(cnt):
            v = v_buf[idx][s] - v_offset
            i = i_buf[idx][s] - i_offset
            if abs(i) < low_pass:
                low_samples += 1
            p_sum += i * v
            v_rms_sum += v * v

        # pcb calibration factors are both 1.0 for LPMPCB1, so they cancel out
        i_cal = b.get('port_cal', 1.0) * b.get('breaker_cal', 1.0)

        v_rms = voltage_cal * math.sqrt(v_rms_sum / cnt)
        real_power = (voltage_cal * i_cal * p_sum) / cnt

        # Low-pass noise filter: zero out weak signals
        if (low_samples * 100 // cnt > 75) and abs(real_power) < 13.0:
            real_power = 0.0

        # Apply polarity
        polarity = b.get('polarity', 'NORMAL')
        if polarity == 'NORMAL':
            real_power = abs(real_power)
        elif polarity == 'SOLAR':
            real_power = -abs(real_power)
        elif polarity == 'BI_DIRECTIONAL_INVERTED':
            real_power = -real_power
        # BI_DIRECTIONAL: leave signed as-is

        if b.get('double_power', False):
            real_power *= 2.0

        results.append({
            'panel': b['panel'],
            'space': b['space'],
            'power': real_power,
            'voltage': v_rms,
            'samples': cnt,
        })

    return results


def calibrate_voltage(adc, current_cal, frequency=60, duration_s=2.0):
    """
    Measure voltage for duration_s seconds and compute a new calibration factor.

    Mirrors Java PowerMonitor.calibrateVoltage(). Counts AC zero-crossings to
    detect frequency, then scales the factor so that sqrt(raw_rms) * new_cal = 120V.

    Args:
        adc:         ADCReader instance
        current_cal: existing voltage calibration factor (hub.voltageCalibrationFactor)
        frequency:   expected AC frequency in Hz (used for fallback only)
        duration_s:  how long to sample (default 2 seconds)

    Returns:
        (new_cal_factor, detected_frequency) or (None, None) on failure
    """
    MAX_SAMPLES = 300000
    v_raw = array('H', bytes(MAX_SAMPLES * 2))
    times = array('d', [0.0] * MAX_SAMPLES)
    count = 0

    end_time = time.monotonic() + duration_s
    while count < MAX_SAMPLES and time.monotonic() < end_time:
        times[count] = time.monotonic()
        v_raw[count] = adc.read_voltage()
        count += 1

    if count == 0:
        return None, None

    # DC offset
    v_offset = sum(v_raw[:count]) / count

    # Count AC cycles by detecting rising zero-crossings through the offset.
    # Matches Java: if first sample is already above threshold, count it as cycle 1.
    if v_raw[0] > (v_offset * 1.3):
        cycles = 1
        under = False
    else:
        cycles = 0
        under = True
    v_rms_sum = 0.0

    for s in range(count):
        v = v_raw[s] - v_offset
        v_rms_sum += v * v
        if under and v_raw[s] > (v_offset * 1.3):
            cycles += 1
            under = False
        elif v_raw[s] < (v_offset * 0.7):
            under = True

    raw_rms = math.sqrt(v_rms_sum / count)
    old_vrms = current_cal * raw_rms

    if old_vrms < 20:
        print("ERROR: Could not get a valid voltage read. Check AC/AC transformer connection.")
        return None, None

    elapsed_s = times[count - 1] - times[0]
    detected_freq = round(cycles / elapsed_s / 10) * 10 if elapsed_s > 0 else frequency
    target_v = 120 if detected_freq > 55 else 230
    new_cal = (target_v / old_vrms) * current_cal

    print(f"Calibration: detected {detected_freq}Hz, old Vrms={old_vrms:.3f}, "
          f"new_cal={new_cal:.6f}, new Vrms={new_cal * raw_rms:.3f}V")

    return new_cal, detected_freq
