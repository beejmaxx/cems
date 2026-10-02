// Bounded native framing -> hexadecimal Hashcat wordlist bridge.
// No checking/coverage decisions. Unsupported bytes fail the ENTIRE assignment.
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unistd.h>

static void need(bool ok, const char *why) { if (!ok) throw std::runtime_error(why); }
int main(int argc, char **argv) {
  try {
    need(argc == 2, "usage: hex-spool EXPECTED_COUNT < frames > candidates.hex");
    std::string arg = argv[1]; uint64_t expected = 0;
    need(!arg.empty() && arg.size() <= 7, "count bound");
    for (char c : arg) { need(c >= '0' && c <= '9', "invalid count"); expected = expected*10+uint64_t(c-'0'); }
    need(expected > 0 && expected <= 1000000, "batch bound: 1..1000000");
    std::array<char, 65536> in{}, out{};
    size_t used = 0; uint64_t count = 0, written = 0; int remaining = -1;
    const char *hex = "0123456789abcdef";
    auto flush = [&]() { need(std::fwrite(out.data(), 1, used, stdout) == used, "write failed"); written += used; used = 0; };
    auto put = [&](char c) { if (used == out.size()) flush(); out[used++] = c; };
    for (;;) {
      auto n = read(STDIN_FILENO, in.data(), in.size());
      if (n < 0) { if (errno == EINTR) continue; throw std::runtime_error("read failed"); }
      if (!n) break;
      for (ssize_t i = 0; i < n; ++i) {
        auto c = static_cast<unsigned char>(in[size_t(i)]);
        if (remaining < 0) {
          need(count < expected && c >= 1 && c <= 64, "GPU bridge admits nonempty 1..64 byte candidates only");
          remaining = c;
        } else {
          need(c >= 0x20 && c <= 0x7e, "GPU bridge admits printable ASCII only; nothing credited");
          put(hex[c >> 4]); put(hex[c & 15]);
          if (--remaining == 0) { put('\n'); ++count; remaining = -1; }
        }
      }
    }
    need(remaining == -1 && count == expected, "truncated/missing frames");
    flush(); need(std::fflush(stdout) == 0, "flush failed");
    std::cerr << "{\"frames\":" << count << ",\"bytes\":" << written << "}\n";
    return 0;
  } catch (const std::exception &e) { std::cerr << "hex-spool: " << e.what() << "\n"; return 1; }
}
