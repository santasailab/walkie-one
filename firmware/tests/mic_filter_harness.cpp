#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>

#include "mic_filter.h"

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr int kSampleRate = 24000;

double responseAt(double frequency) {
  MicHighPass filter;
  double inputEnergy = 0.0;
  double outputEnergy = 0.0;
  const int settleSamples = kSampleRate / 2;
  const int totalSamples = kSampleRate * 2;

  for (int i = 0; i < totalSamples; ++i) {
    const auto input = static_cast<int32_t>(
        std::lround(12000.0 * std::sin(2.0 * kPi * frequency * i /
                                      kSampleRate)));
    const int32_t output = filter.process(input);
    if (i >= settleSamples) {
      inputEnergy += static_cast<double>(input) * input;
      outputEnergy += static_cast<double>(output) * output;
    }
  }
  return std::sqrt(outputEnergy / inputEnergy);
}

bool between(double value, double low, double high) {
  return value >= low && value <= high;
}

}  // namespace

int main() {
  const double at20 = responseAt(20.0);
  const double at100 = responseAt(100.0);
  const double at300 = responseAt(300.0);
  const double at1000 = responseAt(1000.0);

  std::cout << "20Hz=" << at20 << " 100Hz=" << at100
            << " 300Hz=" << at300 << " 1000Hz=" << at1000 << '\n';

  if (!between(at20, 0.05, 0.15)) return EXIT_FAILURE;
  if (!between(at100, 0.65, 0.80)) return EXIT_FAILURE;
  if (!between(at300, 0.90, 1.00)) return EXIT_FAILURE;
  if (!between(at1000, 0.95, 1.00)) return EXIT_FAILURE;
  return EXIT_SUCCESS;
}
