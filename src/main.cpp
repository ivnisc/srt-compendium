#include "srt_runtime.h"

#include <exception>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 2) {
        srt_pa::print_usage();
        return 2;
    }

    try {
        const std::string command = argv[1];
        if (command == "send") {
            return srt_pa::run_sender(argc - 1, argv + 1);
        }
        if (command == "recv") {
            return srt_pa::run_receiver(argc - 1, argv + 1);
        }
        if (command == "--help" || command == "help") {
            srt_pa::print_usage();
            return 0;
        }
        std::cerr << "Comando desconocido: " << command << "\n";
        srt_pa::print_usage();
        return 2;
    } catch (const std::exception& error) {
        std::cerr << "error: " << error.what() << "\n";
        return 1;
    }
}
