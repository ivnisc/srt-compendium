#pragma once

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace srt_pa {

struct KalmanEstimate {
    bool initialized = false;
    double rtt_ms = 0.0;
    double trend_ms_s = 0.0;
    double predicted_rtt_ms = 0.0;
    double innovation = 0.0;
    double nis = 0.0;
};

class RttTrendKalman {
public:
    RttTrendKalman(double process_noise, double measurement_noise,
                   double initial_variance = 100.0,
                   bool allow_negative_measurements = false)
        : q_(process_noise), r_(measurement_noise), p00_(initial_variance),
          p11_(initial_variance),
          allow_negative_measurements_(allow_negative_measurements) {
        if (q_ < 0.0 || r_ <= 0.0 || initial_variance <= 0.0) {
            throw std::invalid_argument("Parámetros de Kalman inválidos");
        }
    }

    KalmanEstimate update(double measurement_ms, double dt_s,
                          double horizon_s) {
        if (!std::isfinite(measurement_ms) ||
            (!allow_negative_measurements_ && measurement_ms < 0.0)) {
            return estimate(horizon_s);
        }

        if (!initialized_) {
            x0_ = measurement_ms;
            x1_ = 0.0;
            initialized_ = true;
            return estimate(horizon_s);
        }

        dt_s = std::max(dt_s, 1e-6);

        // predicción con modelo local de nivel y tendencia
        const double x0_pred = x0_ + dt_s * x1_;
        const double x1_pred = x1_;

        const double q00 = q_ * dt_s * dt_s * dt_s / 3.0;
        const double q01 = q_ * dt_s * dt_s / 2.0;
        const double q11 = q_ * dt_s;

        const double pp00 = p00_ + dt_s * (p10_ + p01_) +
                            dt_s * dt_s * p11_ + q00;
        const double pp01 = p01_ + dt_s * p11_ + q01;
        const double pp10 = p10_ + dt_s * p11_ + q01;
        const double pp11 = p11_ + q11;

        innovation_ = measurement_ms - x0_pred;
        const double innovation_variance = pp00 + r_;
        const double k0 = pp00 / innovation_variance;
        const double k1 = pp10 / innovation_variance;

        x0_ = x0_pred + k0 * innovation_;
        x1_ = x1_pred + k1 * innovation_;

        // forma de Joseph simplificada para H=[1,0]
        p00_ = (1.0 - k0) * pp00;
        p01_ = (1.0 - k0) * pp01;
        p10_ = pp10 - k1 * pp00;
        p11_ = pp11 - k1 * pp01;

        nis_ = innovation_ * innovation_ / innovation_variance;
        return estimate(horizon_s);
    }

    KalmanEstimate estimate(double horizon_s) const {
        KalmanEstimate out;
        out.initialized = initialized_;
        out.rtt_ms = x0_;
        out.trend_ms_s = x1_;
        out.predicted_rtt_ms = x0_ + std::max(0.0, horizon_s) * x1_;
        out.innovation = innovation_;
        out.nis = nis_;
        return out;
    }

private:
    double q_;
    double r_;
    bool initialized_ = false;
    double x0_ = 0.0;
    double x1_ = 0.0;
    double p00_ = 0.0;
    double p01_ = 0.0;
    double p10_ = 0.0;
    double p11_ = 0.0;
    double innovation_ = 0.0;
    double nis_ = 0.0;
    bool allow_negative_measurements_ = false;
};

} // namespace srt_pa
