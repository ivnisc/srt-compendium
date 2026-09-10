#include "srt_runtime.h"

#include "kalman.h"

#include <srt.h>

#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace srt_pa {
namespace {

using Clock = std::chrono::steady_clock;

class Arguments {
public:
    Arguments(int argc, char** argv) {
        for (int i = 1; i < argc; ++i) {
            const std::string token = argv[i];
            if (token.rfind("--", 0) != 0) {
                throw std::invalid_argument("Argumento posicional inesperado: " + token);
            }
            if (i + 1 < argc && std::string(argv[i + 1]).rfind("--", 0) != 0) {
                values_[token.substr(2)] = argv[++i];
            } else {
                values_[token.substr(2)] = "true";
            }
        }
    }

    bool has(const std::string& key) const { return values_.count(key) != 0; }

    std::string get(const std::string& key, const std::string& fallback = "") const {
        const auto it = values_.find(key);
        return it == values_.end() ? fallback : it->second;
    }

    std::string require(const std::string& key) const {
        const auto value = get(key);
        if (value.empty()) {
            throw std::invalid_argument("Falta --" + key);
        }
        return value;
    }

    int get_int(const std::string& key, int fallback) const {
        return has(key) ? std::stoi(get(key)) : fallback;
    }

    int64_t get_i64(const std::string& key, int64_t fallback) const {
        return has(key) ? std::stoll(get(key)) : fallback;
    }

    double get_double(const std::string& key, double fallback) const {
        return has(key) ? std::stod(get(key)) : fallback;
    }

private:
    std::map<std::string, std::string> values_;
};

struct SrtLifecycle {
    SrtLifecycle() {
        if (srt_startup() == SRT_ERROR) {
            throw std::runtime_error("srt_startup falló");
        }
    }
    ~SrtLifecycle() { srt_cleanup(); }
};

struct SocketHandle {
    SRTSOCKET value = SRT_INVALID_SOCK;
    SocketHandle() = default;
    explicit SocketHandle(SRTSOCKET socket) : value(socket) {}
    SocketHandle(const SocketHandle&) = delete;
    SocketHandle& operator=(const SocketHandle&) = delete;
    ~SocketHandle() {
        if (value != SRT_INVALID_SOCK) {
            srt_close(value);
        }
    }
};

std::string srt_error(const std::string& context) {
    return context + ": " + srt_getlasterror_str();
}

template <typename T>
void set_option(SRTSOCKET socket, SRT_SOCKOPT option, const T& value,
                const std::string& name) {
    if (srt_setsockopt(socket, 0, option, &value, sizeof(value)) == SRT_ERROR) {
        throw std::runtime_error(srt_error("No se pudo configurar " + name));
    }
}

sockaddr_storage resolve_address(const std::string& host, int port, bool passive,
                                 int& address_length) {
    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_DGRAM;
    hints.ai_flags = passive ? AI_PASSIVE : 0;

    addrinfo* result = nullptr;
    const std::string service = std::to_string(port);
    const char* node = passive && (host.empty() || host == "0.0.0.0")
                           ? nullptr
                           : host.c_str();
    const int status = getaddrinfo(node, service.c_str(), &hints, &result);
    if (status != 0 || result == nullptr) {
        throw std::runtime_error("No se pudo resolver " + host + ":" + service);
    }

    sockaddr_storage storage{};
    std::memcpy(&storage, result->ai_addr, result->ai_addrlen);
    address_length = static_cast<int>(result->ai_addrlen);
    freeaddrinfo(result);
    return storage;
}

void configure_live_socket(SRTSOCKET socket, int latency_ms, int timeout_ms,
                           bool receiver) {
    const SRT_TRANSTYPE type = SRTT_LIVE;
    const bool enabled = true;
    const int payload_size = 1316;
    set_option(socket, SRTO_TRANSTYPE, type, "SRTO_TRANSTYPE");
    set_option(socket, SRTO_TSBPDMODE, enabled, "SRTO_TSBPDMODE");
    set_option(socket, SRTO_TLPKTDROP, enabled, "SRTO_TLPKTDROP");
    set_option(socket, SRTO_PAYLOADSIZE, payload_size, "SRTO_PAYLOADSIZE");
    set_option(socket, SRTO_RCVLATENCY, latency_ms, "SRTO_RCVLATENCY");
    set_option(socket, SRTO_PEERLATENCY, latency_ms, "SRTO_PEERLATENCY");
    if (receiver) {
        set_option(socket, SRTO_RCVTIMEO, timeout_ms, "SRTO_RCVTIMEO");
    } else {
        set_option(socket, SRTO_SNDTIMEO, timeout_ms, "SRTO_SNDTIMEO");
    }
}

void write_signal_file(const std::string& path) {
    if (path.empty()) {
        return;
    }
    const std::filesystem::path output(path);
    if (output.has_parent_path()) {
        std::filesystem::create_directories(output.parent_path());
    }
    std::ofstream stream(path, std::ios::trunc);
    if (!stream) {
        throw std::runtime_error("No se pudo escribir señal: " + path);
    }
    const auto now = std::chrono::duration_cast<std::chrono::microseconds>(
                         Clock::now().time_since_epoch())
                         .count();
    stream << now << "\n";
}

struct ControllerConfig {
    std::string mode = "vanilla";
    int nominal_ohead = 25;
    int active_ohead = 25;
    int active_from_ms = 15000;
    int active_until_ms = 45000;
    int warmup_ms = 3000;
    int cooldown_ms = 1000;
    int trigger_samples = 3;
    int release_samples = 5;
    double trigger_rtt_ms = std::numeric_limits<double>::quiet_NaN();
    double release_rtt_ms = std::numeric_limits<double>::quiet_NaN();
    double horizon_ms = 250.0;
    double process_noise = 1.0;
    double measurement_noise = 4.0;
    int v2_baseline_begin_ms = 3000;
    int v2_baseline_end_ms = 10000;
    double v2_q_rtt = 10.0;
    double v2_q_sndbuf = 10.0;
    double v2_rtt_weight = 0.75;
    double v2_trigger_score = 2.5;
    int v2_trigger_samples = 3;
    double v2_release_ratio = 0.5;
    int v2_release_samples = 10;
    double v2_confirmation_ratio = 5.0;
    int v2_confirmation_timeout_ms = 5000;
    int v2_retry_cooldown_ms = 1000;
    bool v2_actuate_on_confirmation = false;
};

struct V2ControllerEstimate {
    bool ready = false;
    double rtt_baseline = 0.0;
    double rtt_sigma = 0.0;
    double sndbuf_baseline = 0.0;
    double sndbuf_sigma = 0.0;
    double rtt_level = 0.0;
    double rtt_trend = 0.0;
    double sndbuf_level = 0.0;
    double sndbuf_trend = 0.0;
    double score = 0.0;
    bool confirmed = false;
};

struct MonitorAggregate {
    SRT_TRACEBSTATS last{};
    int64_t belated_sum = 0;
    int64_t rcv_retrans_sum = 0;
    int peak_ms_snd_buf = 0;
    int final_ohead = 0;
    int controller_changes = 0;
    bool v2_ready = false;
    bool v2_confirmed = false;
    double v2_rtt_baseline = 0.0;
    double v2_rtt_sigma = 0.0;
    double v2_sndbuf_baseline = 0.0;
    double v2_sndbuf_sigma = 0.0;
    double v2_score = 0.0;
    bool has_sample = false;
};

class StatsMonitor {
public:
    StatsMonitor(SRTSOCKET socket, std::string role, std::string variant,
                 std::string output_path, int sample_ms,
                 ControllerConfig controller)
        : socket_(socket), role_(std::move(role)), variant_(std::move(variant)),
          output_path_(std::move(output_path)), sample_ms_(sample_ms),
          controller_(std::move(controller)),
          kalman_(controller_.process_noise, controller_.measurement_noise),
          current_ohead_(controller_.nominal_ohead) {
        if (sample_ms_ <= 0) {
            throw std::invalid_argument("sample-ms debe ser positivo");
        }
    }

    void start() {
        const std::filesystem::path output(output_path_);
        if (output.has_parent_path()) {
            std::filesystem::create_directories(output.parent_path());
        }
        stream_.open(output_path_, std::ios::trunc);
        if (!stream_) {
            throw std::runtime_error("No se pudo abrir CSV de estadísticas: " + output_path_);
        }
        stream_ << std::setprecision(15);
        write_header();
        start_time_ = Clock::now();
        last_sample_time_ = start_time_;
        last_filter_time_ = start_time_;
        last_change_time_ = start_time_ -
            std::chrono::milliseconds(controller_.cooldown_ms);
        worker_ = std::thread([this] { loop(); });
    }

    void stop() {
        stop_requested_.store(true);
        if (worker_.joinable()) {
            worker_.join();
        }
        if (role_ == "sender" && current_ohead_ != controller_.nominal_ohead) {
            std::string decision = "stop_restore";
            set_ohead(controller_.nominal_ohead, decision);
            std::lock_guard<std::mutex> lock(aggregate_mutex_);
            aggregate_.final_ohead = current_ohead_;
            aggregate_.controller_changes = controller_changes_;
        }
        stream_.flush();
        stream_.close();
    }

    MonitorAggregate aggregate() const {
        std::lock_guard<std::mutex> lock(aggregate_mutex_);
        return aggregate_;
    }

private:
    void write_header() {
        stream_ << "elapsed_ms,sample_interval_us,sample_lateness_us,srt_ms,role,variant,decision,ohead_pct,"
                   "kf_initialized,kf_rtt_ms,kf_trend_ms_s,predicted_rtt_ms,innovation,nis,"
                   "v2_eval_us,v2_ready,v2_rtt_baseline,v2_rtt_sigma,v2_sndbuf_baseline,v2_sndbuf_sigma,"
                   "v2_rtt_level,v2_rtt_trend_s,v2_sndbuf_level,v2_sndbuf_trend_s,v2_score,v2_confirmed,"
                   "pktSent,pktRecv,pktSentUnique,pktRecvUnique,pktSndLoss,pktRcvLoss,"
                   "pktRetrans,pktRcvRetrans,pktRcvBelated,pktSndDrop,pktRcvDrop,"
                   "byteSent,byteRecv,byteSentUnique,byteRecvUnique,byteRetrans,byteSndDrop,byteRcvDrop,"
                   "pktSentTotal,pktRecvTotal,pktSentUniqueTotal,pktRecvUniqueTotal,"
                   "pktSndLossTotal,pktRcvLossTotal,pktRetransTotal,pktSndDropTotal,pktRcvDropTotal,"
                   "byteSentTotal,byteRecvTotal,byteSentUniqueTotal,byteRecvUniqueTotal,"
                   "byteRetransTotal,byteSndDropTotal,byteRcvDropTotal,"
                   "mbpsSendRate,mbpsRecvRate,msRTT,mbpsBandwidth,mbpsMaxBW,usPktSndPeriod,"
                   "pktSndBuf,byteSndBuf,msSndBuf,pktRcvBuf,byteRcvBuf,msRcvBuf\n";
    }

    bool set_ohead(int value, std::string& decision) {
        if (role_ != "sender" || value == current_ohead_) {
            return true;
        }
        const int32_t option_value = value;
        if (srt_setsockopt(socket_, 0, SRTO_OHEADBW, &option_value,
                           sizeof(option_value)) == SRT_ERROR) {
            decision = "set_ohead_error";
            return false;
        }
        current_ohead_ = value;
        ++controller_changes_;
        last_change_time_ = Clock::now();
        return true;
    }

    static double median_value(std::vector<double> values) {
        if (values.empty()) {
            return 0.0;
        }
        std::sort(values.begin(), values.end());
        const std::size_t middle = values.size() / 2;
        if (values.size() % 2 != 0) {
            return values[middle];
        }
        return 0.5 * (values[middle - 1] + values[middle]);
    }

    static double sample_stddev(const std::vector<double>& values) {
        if (values.size() < 2) {
            return 0.0;
        }
        double average = 0.0;
        for (const double value : values) {
            average += value;
        }
        average /= static_cast<double>(values.size());
        double sum = 0.0;
        for (const double value : values) {
            const double difference = value - average;
            sum += difference * difference;
        }
        return std::sqrt(sum / static_cast<double>(values.size() - 1));
    }

    V2ControllerEstimate update_v2(int64_t elapsed_ms,
                                   const SRT_TRACEBSTATS& stats) {
        V2ControllerEstimate output;
        if (role_ != "sender" || controller_.mode != "assistant_v2") {
            return output;
        }
        if (elapsed_ms >= controller_.v2_baseline_begin_ms &&
            elapsed_ms < controller_.v2_baseline_end_ms && stats.msRTT > 0.0) {
            v2_baseline_elapsed_ms_.push_back(static_cast<double>(elapsed_ms));
            v2_baseline_rtt_.push_back(stats.msRTT);
            v2_baseline_sndbuf_.push_back(static_cast<double>(stats.msSndBuf));
        }
        if (!v2_ready_ && elapsed_ms >= controller_.v2_baseline_end_ms) {
            if (v2_baseline_rtt_.size() < 20) {
                return output;
            }
            v2_rtt_baseline_ = median_value(v2_baseline_rtt_);
            v2_sndbuf_baseline_ = median_value(v2_baseline_sndbuf_);
            v2_rtt_sigma_ = std::max(sample_stddev(v2_baseline_rtt_), 0.01);
            v2_sndbuf_sigma_ = std::max(sample_stddev(v2_baseline_sndbuf_), 1.0);
            v2_rtt_filter_ = std::make_unique<RttTrendKalman>(
                controller_.v2_q_rtt, 1.0, 100.0, true);
            v2_sndbuf_filter_ = std::make_unique<RttTrendKalman>(
                controller_.v2_q_sndbuf, 1.0, 100.0, true);
            double previous = static_cast<double>(controller_.v2_baseline_begin_ms);
            for (std::size_t index = 0; index < v2_baseline_rtt_.size(); ++index) {
                const double elapsed = v2_baseline_elapsed_ms_[index];
                const double dt_s = std::max((elapsed - previous) / 1000.0, 1e-6);
                v2_rtt_estimate_ = v2_rtt_filter_->update(
                    (v2_baseline_rtt_[index] - v2_rtt_baseline_) / v2_rtt_sigma_,
                    dt_s, controller_.horizon_ms / 1000.0);
                v2_sndbuf_estimate_ = v2_sndbuf_filter_->update(
                    (v2_baseline_sndbuf_[index] - v2_sndbuf_baseline_) /
                        v2_sndbuf_sigma_,
                    dt_s, controller_.horizon_ms / 1000.0);
                previous = elapsed;
            }
            v2_last_filter_elapsed_ms_ = previous;
            v2_ready_ = true;
        }
        if (v2_ready_) {
            const double dt_s = std::max(
                (static_cast<double>(elapsed_ms) - v2_last_filter_elapsed_ms_) /
                    1000.0,
                1e-6);
            v2_rtt_estimate_ = v2_rtt_filter_->update(
                (stats.msRTT - v2_rtt_baseline_) / v2_rtt_sigma_,
                dt_s, controller_.horizon_ms / 1000.0);
            v2_sndbuf_estimate_ = v2_sndbuf_filter_->update(
                (static_cast<double>(stats.msSndBuf) - v2_sndbuf_baseline_) /
                    v2_sndbuf_sigma_,
                dt_s, controller_.horizon_ms / 1000.0);
            v2_last_filter_elapsed_ms_ = static_cast<double>(elapsed_ms);
            v2_score_ = controller_.v2_rtt_weight *
                            v2_rtt_estimate_.predicted_rtt_ms +
                        (1.0 - controller_.v2_rtt_weight) *
                            v2_sndbuf_estimate_.predicted_rtt_ms;
        }
        output.ready = v2_ready_;
        output.rtt_baseline = v2_rtt_baseline_;
        output.rtt_sigma = v2_rtt_sigma_;
        output.sndbuf_baseline = v2_sndbuf_baseline_;
        output.sndbuf_sigma = v2_sndbuf_sigma_;
        output.rtt_level = v2_rtt_estimate_.rtt_ms;
        output.rtt_trend = v2_rtt_estimate_.trend_ms_s;
        output.sndbuf_level = v2_sndbuf_estimate_.rtt_ms;
        output.sndbuf_trend = v2_sndbuf_estimate_.trend_ms_s;
        output.score = v2_score_;
        output.confirmed = v2_confirmed_;
        return output;
    }

    std::string apply_v2_controller(int64_t elapsed_ms,
                                    const V2ControllerEstimate& estimate) {
        if (!estimate.ready) {
            return "v2_calibrate";
        }
        if (v2_completed_) {
            return "v2_complete";
        }
        std::string decision = "v2_hold";
        if (!v2_active_) {
            if (elapsed_ms < v2_retry_after_ms_) {
                return "v2_retry_cooldown";
            }
            v2_trigger_count_ = estimate.score >= controller_.v2_trigger_score
                                    ? v2_trigger_count_ + 1 : 0;
            if (v2_trigger_count_ >= controller_.v2_trigger_samples) {
                decision = "v2_activate";
                if (set_ohead(controller_.active_ohead, decision)) {
                    v2_active_ = true;
                    v2_confirmed_ = false;
                    v2_active_since_ms_ = elapsed_ms;
                    v2_trigger_count_ = 0;
                }
            }
            return decision;
        }

        if (!v2_confirmed_ && estimate.score >=
                controller_.v2_trigger_score * controller_.v2_confirmation_ratio) {
            v2_confirmed_ = true;
            decision = "v2_confirm";
        }
        if (!v2_confirmed_ && elapsed_ms - v2_active_since_ms_ >=
                controller_.v2_confirmation_timeout_ms) {
            decision = "v2_unconfirmed_restore";
            if (set_ohead(controller_.nominal_ohead, decision)) {
                v2_active_ = false;
                v2_retry_after_ms_ = elapsed_ms + controller_.v2_retry_cooldown_ms;
            }
            return decision;
        }
        if (v2_confirmed_) {
            v2_release_count_ = estimate.score <=
                    controller_.v2_trigger_score * controller_.v2_release_ratio
                ? v2_release_count_ + 1 : 0;
            if (v2_release_count_ >= controller_.v2_release_samples) {
                decision = "v2_restore";
                if (set_ohead(controller_.nominal_ohead, decision)) {
                    v2_active_ = false;
                    v2_completed_ = true;
                    v2_release_count_ = 0;
                }
            }
        }
        return decision;
    }

    std::string apply_v2_confirmed_controller(
            int64_t elapsed_ms, const V2ControllerEstimate& estimate) {
        if (!estimate.ready) {
            return "v2_calibrate";
        }
        if (v2_completed_) {
            return "v2_complete";
        }
        std::string decision = "v2_hold";
        if (v2_active_) {
            v2_release_count_ = estimate.score <=
                    controller_.v2_trigger_score * controller_.v2_release_ratio
                ? v2_release_count_ + 1 : 0;
            if (v2_release_count_ >= controller_.v2_release_samples) {
                decision = "v2_restore";
                if (set_ohead(controller_.nominal_ohead, decision)) {
                    v2_active_ = false;
                    v2_completed_ = true;
                    v2_release_count_ = 0;
                }
            }
            return decision;
        }
        if (v2_candidate_) {
            if (estimate.score >= controller_.v2_trigger_score *
                                      controller_.v2_confirmation_ratio) {
                decision = "v2_activate";
                if (set_ohead(controller_.active_ohead, decision)) {
                    v2_candidate_ = false;
                    v2_active_ = true;
                    v2_confirmed_ = true;
                    v2_active_since_ms_ = elapsed_ms;
                }
            } else if (elapsed_ms - v2_candidate_since_ms_ >=
                       controller_.v2_confirmation_timeout_ms) {
                v2_candidate_ = false;
                v2_retry_after_ms_ = elapsed_ms +
                    controller_.v2_retry_cooldown_ms;
                decision = "v2_candidate_timeout";
            }
            return decision;
        }
        if (elapsed_ms < v2_retry_after_ms_) {
            return "v2_retry_cooldown";
        }
        v2_trigger_count_ = estimate.score >= controller_.v2_trigger_score
                                ? v2_trigger_count_ + 1 : 0;
        if (v2_trigger_count_ >= controller_.v2_trigger_samples) {
            v2_candidate_ = true;
            v2_candidate_since_ms_ = elapsed_ms;
            v2_trigger_count_ = 0;
            decision = "v2_candidate";
        }
        return decision;
    }

    std::string apply_controller(int64_t elapsed_ms, const KalmanEstimate& estimate,
                                 const V2ControllerEstimate& v2_estimate) {
        if (role_ != "sender" || controller_.mode == "vanilla") {
            return "observe";
        }

        std::string decision = "hold";
        if (controller_.mode == "fixed") {
            const bool active = elapsed_ms >= controller_.active_from_ms &&
                                elapsed_ms < controller_.active_until_ms;
            const int target = active ? controller_.active_ohead
                                      : controller_.nominal_ohead;
            if (target != current_ohead_) {
                decision = active ? "fixed_activate" : "fixed_restore";
                set_ohead(target, decision);
            }
            return decision;
        }

        if (controller_.mode == "assistant_v2") {
            if (controller_.v2_actuate_on_confirmation) {
                return apply_v2_confirmed_controller(elapsed_ms, v2_estimate);
            }
            return apply_v2_controller(elapsed_ms, v2_estimate);
        }

        if (controller_.mode != "assistant" || !estimate.initialized ||
            elapsed_ms < controller_.warmup_ms) {
            return "warmup";
        }

        const auto since_change = std::chrono::duration_cast<std::chrono::milliseconds>(
                                      Clock::now() - last_change_time_)
                                      .count();
        if (since_change < controller_.cooldown_ms) {
            return "cooldown";
        }

        if (current_ohead_ == controller_.nominal_ohead) {
            release_count_ = 0;
            if (estimate.predicted_rtt_ms >= controller_.trigger_rtt_ms) {
                ++trigger_count_;
            } else {
                trigger_count_ = 0;
            }
            if (trigger_count_ >= controller_.trigger_samples) {
                decision = "assistant_activate";
                if (set_ohead(controller_.active_ohead, decision)) {
                    trigger_count_ = 0;
                }
            }
        } else {
            trigger_count_ = 0;
            if (estimate.predicted_rtt_ms <= controller_.release_rtt_ms) {
                ++release_count_;
            } else {
                release_count_ = 0;
            }
            if (release_count_ >= controller_.release_samples) {
                decision = "assistant_restore";
                if (set_ohead(controller_.nominal_ohead, decision)) {
                    release_count_ = 0;
                }
            }
        }
        return decision;
    }

    void sample_once(const Clock::time_point scheduled_time) {
        const auto sample_time = Clock::now();
        const auto sample_interval_us = std::chrono::duration_cast<std::chrono::microseconds>(
                                            sample_time - last_sample_time_)
                                            .count();
        const auto raw_lateness_us = std::chrono::duration_cast<std::chrono::microseconds>(
                                         sample_time - scheduled_time)
                                         .count();
        const auto sample_lateness_us = std::max<int64_t>(0, raw_lateness_us);
        last_sample_time_ = sample_time;

        SRT_TRACEBSTATS stats{};
        if (srt_bistats(socket_, &stats, 1, 1) == SRT_ERROR) {
            return;
        }

        const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                    sample_time - start_time_)
                                    .count();
        const double dt_s = std::max(
            1e-6,
            std::chrono::duration<double>(sample_time - last_filter_time_).count());
        last_filter_time_ = sample_time;

        KalmanEstimate estimate;
        if (stats.msRTT > 0.0) {
            estimate = kalman_.update(stats.msRTT, dt_s,
                                      controller_.horizon_ms / 1000.0);
        } else {
            estimate = kalman_.estimate(controller_.horizon_ms / 1000.0);
        }
        const auto v2_started = Clock::now();
        const V2ControllerEstimate v2_estimate = update_v2(elapsed_ms, stats);
        const std::string decision = apply_controller(
            elapsed_ms, estimate, v2_estimate);
        const double v2_eval_us = std::chrono::duration<double, std::micro>(
                                      Clock::now() - v2_started).count();

        stream_ << elapsed_ms << ',' << sample_interval_us << ','
                << sample_lateness_us << ',' << stats.msTimeStamp << ',' << role_ << ','
                << variant_ << ',' << decision << ',' << current_ohead_ << ','
                << (estimate.initialized ? 1 : 0) << ',' << estimate.rtt_ms << ','
                << estimate.trend_ms_s << ',' << estimate.predicted_rtt_ms << ','
                << estimate.innovation << ',' << estimate.nis << ','
                << v2_eval_us << ',' << (v2_estimate.ready ? 1 : 0) << ','
                << v2_estimate.rtt_baseline << ',' << v2_estimate.rtt_sigma << ','
                << v2_estimate.sndbuf_baseline << ',' << v2_estimate.sndbuf_sigma << ','
                << v2_estimate.rtt_level << ',' << v2_estimate.rtt_trend << ','
                << v2_estimate.sndbuf_level << ',' << v2_estimate.sndbuf_trend << ','
                << v2_estimate.score << ',' << (v2_confirmed_ ? 1 : 0) << ','
                << stats.pktSent << ',' << stats.pktRecv << ','
                << stats.pktSentUnique << ',' << stats.pktRecvUnique << ','
                << stats.pktSndLoss << ',' << stats.pktRcvLoss << ','
                << stats.pktRetrans << ',' << stats.pktRcvRetrans << ','
                << stats.pktRcvBelated << ',' << stats.pktSndDrop << ','
                << stats.pktRcvDrop << ',' << stats.byteSent << ','
                << stats.byteRecv << ',' << stats.byteSentUnique << ','
                << stats.byteRecvUnique << ',' << stats.byteRetrans << ','
                << stats.byteSndDrop << ',' << stats.byteRcvDrop << ','
                << stats.pktSentTotal << ',' << stats.pktRecvTotal << ','
                << stats.pktSentUniqueTotal << ',' << stats.pktRecvUniqueTotal << ','
                << stats.pktSndLossTotal << ',' << stats.pktRcvLossTotal << ','
                << stats.pktRetransTotal << ',' << stats.pktSndDropTotal << ','
                << stats.pktRcvDropTotal << ',' << stats.byteSentTotal << ','
                << stats.byteRecvTotal << ',' << stats.byteSentUniqueTotal << ','
                << stats.byteRecvUniqueTotal << ',' << stats.byteRetransTotal << ','
                << stats.byteSndDropTotal << ',' << stats.byteRcvDropTotal << ','
                << stats.mbpsSendRate << ',' << stats.mbpsRecvRate << ','
                << stats.msRTT << ',' << stats.mbpsBandwidth << ','
                << stats.mbpsMaxBW << ',' << stats.usPktSndPeriod << ','
                << stats.pktSndBuf << ',' << stats.byteSndBuf << ','
                << stats.msSndBuf << ',' << stats.pktRcvBuf << ','
                << stats.byteRcvBuf << ',' << stats.msRcvBuf << '\n';

        if (++samples_since_flush_ >= 20) {
            stream_.flush();
            samples_since_flush_ = 0;
        }

        std::lock_guard<std::mutex> lock(aggregate_mutex_);
        aggregate_.last = stats;
        aggregate_.belated_sum += stats.pktRcvBelated;
        aggregate_.rcv_retrans_sum += stats.pktRcvRetrans;
        aggregate_.peak_ms_snd_buf = std::max(aggregate_.peak_ms_snd_buf,
                                              stats.msSndBuf);
        aggregate_.final_ohead = current_ohead_;
        aggregate_.controller_changes = controller_changes_;
        aggregate_.v2_ready = v2_ready_;
        aggregate_.v2_confirmed = v2_confirmed_;
        aggregate_.v2_rtt_baseline = v2_rtt_baseline_;
        aggregate_.v2_rtt_sigma = v2_rtt_sigma_;
        aggregate_.v2_sndbuf_baseline = v2_sndbuf_baseline_;
        aggregate_.v2_sndbuf_sigma = v2_sndbuf_sigma_;
        aggregate_.v2_score = v2_score_;
        aggregate_.has_sample = true;
    }

    void loop() {
        auto next = start_time_;
        while (!stop_requested_.load()) {
            sample_once(next);
            next += std::chrono::milliseconds(sample_ms_);
            std::this_thread::sleep_until(next);
        }
        sample_once(Clock::now());
    }

    SRTSOCKET socket_;
    std::string role_;
    std::string variant_;
    std::string output_path_;
    int sample_ms_;
    ControllerConfig controller_;
    RttTrendKalman kalman_;
    int current_ohead_;
    int trigger_count_ = 0;
    int release_count_ = 0;
    int controller_changes_ = 0;
    int samples_since_flush_ = 0;
    Clock::time_point start_time_{};
    Clock::time_point last_sample_time_{};
    Clock::time_point last_filter_time_{};
    Clock::time_point last_change_time_{};
    std::atomic<bool> stop_requested_{false};
    std::thread worker_;
    std::ofstream stream_;
    mutable std::mutex aggregate_mutex_;
    MonitorAggregate aggregate_{};
    std::vector<double> v2_baseline_elapsed_ms_;
    std::vector<double> v2_baseline_rtt_;
    std::vector<double> v2_baseline_sndbuf_;
    std::unique_ptr<RttTrendKalman> v2_rtt_filter_;
    std::unique_ptr<RttTrendKalman> v2_sndbuf_filter_;
    KalmanEstimate v2_rtt_estimate_{};
    KalmanEstimate v2_sndbuf_estimate_{};
    bool v2_ready_ = false;
    bool v2_candidate_ = false;
    bool v2_active_ = false;
    bool v2_confirmed_ = false;
    bool v2_completed_ = false;
    int v2_trigger_count_ = 0;
    int v2_release_count_ = 0;
    int64_t v2_active_since_ms_ = 0;
    int64_t v2_candidate_since_ms_ = 0;
    int64_t v2_retry_after_ms_ = 0;
    double v2_last_filter_elapsed_ms_ = 0.0;
    double v2_rtt_baseline_ = 0.0;
    double v2_rtt_sigma_ = 0.0;
    double v2_sndbuf_baseline_ = 0.0;
    double v2_sndbuf_sigma_ = 0.0;
    double v2_score_ = 0.0;
};

void write_metadata(const std::string& path,
                    const std::vector<std::pair<std::string, std::string>>& fields) {
    const std::filesystem::path output(path);
    if (output.has_parent_path()) {
        std::filesystem::create_directories(output.parent_path());
    }
    std::ofstream stream(path, std::ios::trunc);
    if (!stream) {
        throw std::runtime_error("No se pudo escribir metadata: " + path);
    }
    stream << "key,value\n";
    for (const auto& field : fields) {
        stream << field.first << ',' << field.second << '\n';
    }
}

std::string as_text(double value) {
    std::ostringstream stream;
    stream << std::setprecision(12) << value;
    return stream.str();
}

std::string as_text(int64_t value) { return std::to_string(value); }
std::string as_text(uint64_t value) { return std::to_string(value); }
std::string as_text(int value) { return std::to_string(value); }

double percentile(std::vector<double> values, double probability) {
    if (values.empty()) {
        return 0.0;
    }
    std::sort(values.begin(), values.end());
    const double position = (values.size() - 1) *
        std::clamp(probability, 0.0, 1.0);
    const auto lower = static_cast<std::size_t>(std::floor(position));
    const auto upper = static_cast<std::size_t>(std::ceil(position));
    if (lower == upper) {
        return values[lower];
    }
    const double fraction = position - static_cast<double>(lower);
    return values[lower] * (1.0 - fraction) + values[upper] * fraction;
}

ControllerConfig controller_from_args(const Arguments& args) {
    ControllerConfig config;
    config.mode = args.get("mode", "vanilla");
    config.nominal_ohead = args.get_int("nominal-ohead", 25);
    config.active_ohead = args.get_int("active-ohead", config.nominal_ohead);
    config.active_from_ms = args.get_int("active-from-ms", 15000);
    config.active_until_ms = args.get_int("active-until-ms", 45000);
    config.warmup_ms = args.get_int("warmup-ms", 3000);
    config.cooldown_ms = args.get_int("cooldown-ms", 1000);
    config.trigger_samples = args.get_int("trigger-samples", 3);
    config.release_samples = args.get_int("release-samples", 5);
    config.trigger_rtt_ms = args.get_double("trigger-rtt-ms",
                                            std::numeric_limits<double>::quiet_NaN());
    config.release_rtt_ms = args.get_double("release-rtt-ms",
                                            std::numeric_limits<double>::quiet_NaN());
    config.horizon_ms = args.get_double("horizon-ms", 250.0);
    config.process_noise = args.get_double("kalman-q", 1.0);
    config.measurement_noise = args.get_double("kalman-r", 4.0);
    config.v2_baseline_begin_ms = args.get_int("v2-baseline-begin-ms", 3000);
    config.v2_baseline_end_ms = args.get_int("v2-baseline-end-ms", 10000);
    config.v2_q_rtt = args.get_double("v2-q-rtt", 10.0);
    config.v2_q_sndbuf = args.get_double("v2-q-sndbuf", 10.0);
    config.v2_rtt_weight = args.get_double("v2-rtt-weight", 0.75);
    config.v2_trigger_score = args.get_double("v2-trigger-score", 2.5);
    config.v2_trigger_samples = args.get_int("v2-trigger-samples", 3);
    config.v2_release_ratio = args.get_double("v2-release-ratio", 0.5);
    config.v2_release_samples = args.get_int("v2-release-samples", 10);
    config.v2_confirmation_ratio = args.get_double(
        "v2-confirmation-ratio", 5.0);
    config.v2_confirmation_timeout_ms = args.get_int(
        "v2-confirmation-timeout-ms", 5000);
    config.v2_retry_cooldown_ms = args.get_int("v2-retry-cooldown-ms", 1000);
    config.v2_actuate_on_confirmation =
        args.get_int("v2-actuate-on-confirmation", 0) != 0;

    if (config.mode != "vanilla" && config.mode != "fixed" &&
        config.mode != "assistant" && config.mode != "assistant_v2") {
        throw std::invalid_argument(
            "mode debe ser vanilla, fixed, assistant o assistant_v2");
    }
    if (config.nominal_ohead < 5 || config.nominal_ohead > 100 ||
        config.active_ohead < 5 || config.active_ohead > 100) {
        throw std::invalid_argument("OHEADBW debe estar entre 5 y 100");
    }
    if (config.mode == "assistant" &&
        (!std::isfinite(config.trigger_rtt_ms) ||
         !std::isfinite(config.release_rtt_ms) ||
         config.release_rtt_ms >= config.trigger_rtt_ms)) {
        throw std::invalid_argument(
            "assistant requiere --trigger-rtt-ms mayor que --release-rtt-ms");
    }
    if (config.mode == "assistant_v2" &&
        (config.v2_baseline_begin_ms < 0 ||
         config.v2_baseline_end_ms <= config.v2_baseline_begin_ms ||
         config.v2_q_rtt < 0.0 || config.v2_q_sndbuf < 0.0 ||
         config.v2_rtt_weight < 0.0 || config.v2_rtt_weight > 1.0 ||
         config.v2_trigger_score <= 0.0 || config.v2_trigger_samples <= 0 ||
         config.v2_release_ratio < 0.0 || config.v2_release_ratio >= 1.0 ||
         config.v2_release_samples <= 0 ||
         config.v2_confirmation_ratio <= 1.0 ||
         config.v2_confirmation_timeout_ms <= 0 ||
         config.v2_retry_cooldown_ms < 0)) {
        throw std::invalid_argument("Parámetros de assistant_v2 inválidos");
    }
    return config;
}

} // namespace

int run_sender(int argc, char** argv) {
    const Arguments args(argc, argv);
    SrtLifecycle lifecycle;

    const std::string host = args.get("host", "127.0.0.1");
    const int port = args.get_int("port", 9000);
    const std::string input_path = args.require("input");
    const std::string stats_path = args.require("stats");
    const std::string metadata_path = args.require("metadata");
    const std::string variant = args.get("variant", "vanilla");
    const int duration_s = args.get_int("duration-s", 60);
    const int latency_ms = args.get_int("latency-ms", 120);
    const int timeout_ms = args.get_int("timeout-ms", 3000);
    const int sample_ms = args.get_int("sample-ms", 100);
    const int drain_ms = args.get_int("drain-ms", latency_ms + 200);
    const int64_t input_rate_bps = args.get_i64("input-rate-bps", 8500000);
    const ControllerConfig controller = controller_from_args(args);

    if (duration_s <= 0 || input_rate_bps <= 0) {
        throw std::invalid_argument("duration-s e input-rate-bps deben ser positivos");
    }

    std::ifstream input(input_path, std::ios::binary);
    if (!input) {
        throw std::runtime_error("No se pudo abrir la fuente: " + input_path);
    }

    SocketHandle socket(srt_create_socket());
    if (socket.value == SRT_INVALID_SOCK) {
        throw std::runtime_error(srt_error("srt_create_socket"));
    }
    configure_live_socket(socket.value, latency_ms, timeout_ms, false);

    const int64_t max_bw = 0;
    const int64_t input_bw_bytes_s = (input_rate_bps + 7) / 8;
    const int32_t nominal_ohead = controller.nominal_ohead;
    set_option(socket.value, SRTO_MAXBW, max_bw, "SRTO_MAXBW");
    set_option(socket.value, SRTO_INPUTBW, input_bw_bytes_s, "SRTO_INPUTBW");
    set_option(socket.value, SRTO_OHEADBW, nominal_ohead, "SRTO_OHEADBW");

    int address_length = 0;
    const sockaddr_storage address = resolve_address(host, port, false,
                                                     address_length);
    if (srt_connect(socket.value, reinterpret_cast<const sockaddr*>(&address),
                    address_length) == SRT_ERROR) {
        throw std::runtime_error(srt_error("srt_connect"));
    }

    StatsMonitor monitor(socket.value, "sender", variant, stats_path, sample_ms,
                         controller);
    monitor.start();
    write_signal_file(args.get("start-signal"));

    constexpr std::size_t payload_size = 1316;
    std::vector<char> buffer(payload_size);
    uint64_t payload_read = 0;
    uint64_t payload_sent = 0;
    std::vector<double> pacing_lateness_us;
    std::vector<double> send_call_us;
    const uint64_t raw_budget =
        static_cast<uint64_t>(input_rate_bps) * static_cast<uint64_t>(duration_s) / 8;
    const uint64_t payload_budget = raw_budget - (raw_budget % payload_size);
    const uint64_t input_size = std::filesystem::file_size(input_path);
    if (input_size < payload_budget) {
        throw std::runtime_error(
            "La fuente no contiene los bytes requeridos por bitrate y duración");
    }
    const auto started = Clock::now();
    auto payload_finished = started;

    while (payload_sent < payload_budget) {
        const auto remaining = payload_budget - payload_sent;
        const auto requested = static_cast<std::streamsize>(
            std::min<uint64_t>(payload_size, remaining));
        input.read(buffer.data(), requested);
        const std::streamsize count = input.gcount();
        if (count != requested) {
            throw std::runtime_error("La fuente terminó antes del presupuesto fijado");
        }
        const uint64_t projected_payload =
            payload_read + static_cast<uint64_t>(count);
        const double scheduled_seconds =
            static_cast<double>(projected_payload) * 8.0 /
            static_cast<double>(input_rate_bps);
        const auto scheduled_time = started +
            std::chrono::duration_cast<Clock::duration>(
                std::chrono::duration<double>(scheduled_seconds));
        std::this_thread::sleep_until(scheduled_time);
        const auto send_started = Clock::now();
        pacing_lateness_us.push_back(std::max(
            0.0,
            std::chrono::duration<double, std::micro>(
                send_started - scheduled_time).count()));

        payload_read = projected_payload;

        const int sent = srt_sendmsg(socket.value, buffer.data(),
                                     static_cast<int>(count), -1, 1);
        if (sent == SRT_ERROR) {
            throw std::runtime_error(srt_error("srt_sendmsg"));
        }
        const auto send_finished = Clock::now();
        send_call_us.push_back(std::chrono::duration<double, std::micro>(
                                   send_finished - send_started).count());
        payload_sent += static_cast<uint64_t>(sent);
        payload_finished = send_finished;
    }

    std::this_thread::sleep_for(std::chrono::milliseconds(drain_ms));
    monitor.stop();
    const auto finished = Clock::now();
    const MonitorAggregate aggregate = monitor.aggregate();
    const double actual_duration = std::chrono::duration<double>(finished - started).count();

    write_metadata(metadata_path, {
        {"role", "sender"},
        {"variant", variant},
        {"mode", controller.mode},
        {"input_file", input_path},
        {"target_duration_s", as_text(duration_s)},
        {"actual_duration_s", as_text(actual_duration)},
        {"input_rate_bps", as_text(input_rate_bps)},
        {"payload_budget_bytes", as_text(payload_budget)},
        {"payload_bytes_read", as_text(payload_read)},
        {"payload_bytes_sent", as_text(payload_sent)},
        {"payload_send_duration_s", as_text(
            std::chrono::duration<double>(payload_finished - started).count())},
        {"pacing_lateness_p50_us", as_text(percentile(pacing_lateness_us, 0.50))},
        {"pacing_lateness_p95_us", as_text(percentile(pacing_lateness_us, 0.95))},
        {"pacing_lateness_p99_us", as_text(percentile(pacing_lateness_us, 0.99))},
        {"pacing_lateness_max_us", as_text(
            pacing_lateness_us.empty() ? 0.0 :
            *std::max_element(pacing_lateness_us.begin(), pacing_lateness_us.end()))},
        {"send_call_p50_us", as_text(percentile(send_call_us, 0.50))},
        {"send_call_p95_us", as_text(percentile(send_call_us, 0.95))},
        {"send_call_p99_us", as_text(percentile(send_call_us, 0.99))},
        {"send_call_max_us", as_text(
            send_call_us.empty() ? 0.0 :
            *std::max_element(send_call_us.begin(), send_call_us.end()))},
        {"latency_ms", as_text(latency_ms)},
        {"sample_ms", as_text(sample_ms)},
        {"nominal_ohead", as_text(controller.nominal_ohead)},
        {"active_ohead", as_text(controller.active_ohead)},
        {"final_ohead", as_text(aggregate.final_ohead)},
        {"controller_changes", as_text(aggregate.controller_changes)},
        {"v2_ready", as_text(aggregate.v2_ready ? 1 : 0)},
        {"v2_confirmed", as_text(aggregate.v2_confirmed ? 1 : 0)},
        {"v2_rtt_baseline", as_text(aggregate.v2_rtt_baseline)},
        {"v2_rtt_sigma", as_text(aggregate.v2_rtt_sigma)},
        {"v2_sndbuf_baseline", as_text(aggregate.v2_sndbuf_baseline)},
        {"v2_sndbuf_sigma", as_text(aggregate.v2_sndbuf_sigma)},
        {"v2_final_score", as_text(aggregate.v2_score)},
        {"v2_baseline_begin_ms", as_text(controller.v2_baseline_begin_ms)},
        {"v2_baseline_end_ms", as_text(controller.v2_baseline_end_ms)},
        {"v2_q_rtt", as_text(controller.v2_q_rtt)},
        {"v2_q_sndbuf", as_text(controller.v2_q_sndbuf)},
        {"v2_horizon_ms", as_text(controller.horizon_ms)},
        {"v2_rtt_weight", as_text(controller.v2_rtt_weight)},
        {"v2_trigger_score", as_text(controller.v2_trigger_score)},
        {"v2_trigger_samples", as_text(controller.v2_trigger_samples)},
        {"v2_release_ratio", as_text(controller.v2_release_ratio)},
        {"v2_release_samples", as_text(controller.v2_release_samples)},
        {"v2_confirmation_ratio", as_text(controller.v2_confirmation_ratio)},
        {"v2_confirmation_timeout_ms", as_text(
            controller.v2_confirmation_timeout_ms)},
        {"v2_retry_cooldown_ms", as_text(controller.v2_retry_cooldown_ms)},
        {"v2_actuate_on_confirmation", as_text(
            controller.v2_actuate_on_confirmation ? 1 : 0)},
        {"peak_ms_snd_buf", as_text(aggregate.peak_ms_snd_buf)},
        {"byteSentUniqueTotal", as_text(aggregate.last.byteSentUniqueTotal)},
        {"pktSentUniqueTotal", as_text(aggregate.last.pktSentUniqueTotal)},
        {"pktSndDropTotal", as_text(aggregate.last.pktSndDropTotal)},
        {"byteSndDropTotal", as_text(aggregate.last.byteSndDropTotal)},
        {"srt_version_hex", as_text(static_cast<int64_t>(srt_getversion()))}
    });
    return 0;
}

int run_receiver(int argc, char** argv) {
    const Arguments args(argc, argv);
    SrtLifecycle lifecycle;

    const std::string bind_host = args.get("bind", "0.0.0.0");
    const int port = args.get_int("port", 9000);
    const std::string stats_path = args.require("stats");
    const std::string metadata_path = args.require("metadata");
    const std::string variant = args.get("variant", "vanilla");
    const int latency_ms = args.get_int("latency-ms", 120);
    const int timeout_ms = args.get_int("timeout-ms", 500);
    const int sample_ms = args.get_int("sample-ms", 100);
    const int max_duration_s = args.get_int("max-duration-s", 75);

    SocketHandle listener(srt_create_socket());
    if (listener.value == SRT_INVALID_SOCK) {
        throw std::runtime_error(srt_error("srt_create_socket listener"));
    }
    configure_live_socket(listener.value, latency_ms, timeout_ms, true);

    int address_length = 0;
    const sockaddr_storage address = resolve_address(bind_host, port, true,
                                                     address_length);
    if (srt_bind(listener.value, reinterpret_cast<const sockaddr*>(&address),
                 address_length) == SRT_ERROR) {
        throw std::runtime_error(srt_error("srt_bind"));
    }
    if (srt_listen(listener.value, 1) == SRT_ERROR) {
        throw std::runtime_error(srt_error("srt_listen"));
    }
    write_signal_file(args.get("ready-signal"));

    sockaddr_storage peer{};
    int peer_length = sizeof(peer);
    SocketHandle socket(srt_accept(listener.value, reinterpret_cast<sockaddr*>(&peer),
                                   &peer_length));
    if (socket.value == SRT_INVALID_SOCK) {
        throw std::runtime_error(srt_error("srt_accept"));
    }

    ControllerConfig observer;
    observer.mode = "vanilla";
    StatsMonitor monitor(socket.value, "receiver", variant, stats_path,
                         sample_ms, observer);
    monitor.start();

    std::vector<char> buffer(2048);
    uint64_t payload_received = 0;
    std::vector<double> delivery_interval_us;
    Clock::time_point previous_delivery{};
    bool has_previous_delivery = false;
    const auto started = Clock::now();
    const auto deadline = started + std::chrono::seconds(max_duration_s);
    while (Clock::now() < deadline) {
        const int received = srt_recvmsg(socket.value, buffer.data(),
                                         static_cast<int>(buffer.size()));
        if (received > 0) {
            const auto delivered = Clock::now();
            if (has_previous_delivery) {
                delivery_interval_us.push_back(
                    std::chrono::duration<double, std::micro>(
                        delivered - previous_delivery).count());
            }
            previous_delivery = delivered;
            has_previous_delivery = true;
            payload_received += static_cast<uint64_t>(received);
            continue;
        }
        if (received == 0) {
            break;
        }
        const int error_code = srt_getlasterror(nullptr);
        if (error_code == SRT_ETIMEOUT || error_code == SRT_EASYNCRCV) {
            continue;
        }
        if (error_code == SRT_ECONNLOST || error_code == SRT_ENOCONN) {
            break;
        }
        throw std::runtime_error(srt_error("srt_recvmsg"));
    }

    monitor.stop();
    const auto finished = Clock::now();
    const MonitorAggregate aggregate = monitor.aggregate();
    const double actual_duration = std::chrono::duration<double>(finished - started).count();

    write_metadata(metadata_path, {
        {"role", "receiver"},
        {"variant", variant},
        {"mode", "observer"},
        {"actual_duration_s", as_text(actual_duration)},
        {"payload_bytes_received", as_text(payload_received)},
        {"delivery_interval_p50_us", as_text(percentile(delivery_interval_us, 0.50))},
        {"delivery_interval_p95_us", as_text(percentile(delivery_interval_us, 0.95))},
        {"delivery_interval_p99_us", as_text(percentile(delivery_interval_us, 0.99))},
        {"delivery_interval_max_us", as_text(
            delivery_interval_us.empty() ? 0.0 :
            *std::max_element(delivery_interval_us.begin(), delivery_interval_us.end()))},
        {"latency_ms", as_text(latency_ms)},
        {"sample_ms", as_text(sample_ms)},
        {"byteRecvUniqueTotal", as_text(aggregate.last.byteRecvUniqueTotal)},
        {"pktRecvUniqueTotal", as_text(aggregate.last.pktRecvUniqueTotal)},
        {"pktRcvDropTotal", as_text(aggregate.last.pktRcvDropTotal)},
        {"byteRcvDropTotal", as_text(aggregate.last.byteRcvDropTotal)},
        {"pktRcvBelated", as_text(aggregate.belated_sum)},
        {"pktRcvRetrans", as_text(aggregate.rcv_retrans_sum)},
        {"srt_version_hex", as_text(static_cast<int64_t>(srt_getversion()))}
    });
    return 0;
}

void print_usage() {
    std::cout
        << "Uso:\n"
        << "  srt_experiment send --input FILE --stats CSV --metadata CSV [opciones]\n"
        << "  srt_experiment recv --stats CSV --metadata CSV [opciones]\n\n"
        << "Opciones comunes: --port 9000 --latency-ms 120 --sample-ms 100 --variant NOMBRE\n"
        << "Emisor: --host 127.0.0.1 --duration-s 60 --input-rate-bps 8500000\n"
        << "Control: --mode vanilla|fixed|assistant|assistant_v2 --nominal-ohead 25 --active-ohead 10\n"
        << "Fixed: --active-from-ms 15000 --active-until-ms 45000\n"
        << "Assistant: --trigger-rtt-ms X --release-rtt-ms Y --horizon-ms 250\n";
}

} // namespace srt_pa
