cmake_minimum_required(VERSION 3.16)
project(srt_predictive_assistant LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(SRT_ROOT "/Users/ivnisc/Projects/srt" CACHE PATH "Ruta al repositorio de SRT")

find_path(SRT_INCLUDE_DIR
    NAMES srt.h srt/srt.h
    HINTS
        "${SRT_ROOT}/srtcore"
        "${SRT_ROOT}"
        "${SRT_ROOT}/build"
        /usr/local/include
        /opt/homebrew/include
    PATH_SUFFIXES srt
)

find_library(SRT_LIBRARY
    NAMES srt
    HINTS
        "${SRT_ROOT}/build"
        /usr/local/lib
        /opt/homebrew/lib
)

if(NOT SRT_INCLUDE_DIR OR NOT SRT_LIBRARY)
    message(FATAL_ERROR "No se encontró libsrt. Configure -DSRT_ROOT=/ruta/al/repositorio/srt")
endif()

find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)

add_executable(srt_experiment
    src/main.cpp
    src/srt_runtime.cpp
)

target_include_directories(srt_experiment PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${SRT_INCLUDE_DIR}"
    "${SRT_ROOT}/build"
)

target_link_libraries(srt_experiment PRIVATE
    "${SRT_LIBRARY}"
    Threads::Threads
    OpenSSL::SSL
    OpenSSL::Crypto
)

if(APPLE)
    set_target_properties(srt_experiment PROPERTIES
        BUILD_RPATH "${SRT_ROOT}/build"
    )
endif()

target_compile_options(srt_experiment PRIVATE
    $<$<CXX_COMPILER_ID:AppleClang,Clang,GNU>:-Wall;-Wextra;-Wpedantic>
)

include(CTest)
if(BUILD_TESTING)
    add_executable(test_kalman tests/test_kalman.cpp)
    target_include_directories(test_kalman PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src")
    add_test(NAME kalman COMMAND test_kalman)
    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND)
        add_test(NAME analysis
            COMMAND "${Python3_EXECUTABLE}" -m unittest tests/test_analysis.py)
        set_tests_properties(analysis PROPERTIES
            WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
    endif()
    find_program(PDFLATEX_EXECUTABLE pdflatex)
    if(PDFLATEX_EXECUTABLE)
        file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/latex-test")
        add_test(NAME latex_fragments
            COMMAND "${PDFLATEX_EXECUTABLE}" -interaction=nonstopmode
                    -halt-on-error
                    -output-directory "${CMAKE_CURRENT_BINARY_DIR}/latex-test"
                    tests/latex_smoke.tex)
        set_tests_properties(latex_fragments PROPERTIES
            WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
    endif()
endif()
