#include "kalman.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    srt_pa::RttTrendKalman filter(1.0, 4.0);
    srt_pa::KalmanEstimate result;

    for (int i = 0; i < 80; ++i) {
        const double measurement = 50.0 + i * 0.5;
        result = filter.update(measurement, 0.1, 0.5);
    }

    assert(result.initialized);
    assert(std::isfinite(result.nis));
    assert(result.trend_ms_s > 3.0 && result.trend_ms_s < 7.0);
    assert(result.predicted_rtt_ms > result.rtt_ms);

    srt_pa::RttTrendKalman unsigned_filter(1.0, 1.0);
    const auto before_negative = unsigned_filter.update(10.0, 0.1, 0.0);
    const auto after_negative = unsigned_filter.update(-2.0, 0.1, 0.0);
    assert(after_negative.rtt_ms == before_negative.rtt_ms);

    srt_pa::RttTrendKalman signed_filter(1.0, 1.0, 100.0, true);
    signed_filter.update(-1.0, 0.1, 0.0);
    const auto signed_result = signed_filter.update(-2.0, 0.1, 0.0);
    assert(signed_result.initialized);
    assert(signed_result.rtt_ms < -1.0);

    std::cout << "kalman test ok\n";
    return 0;
}
