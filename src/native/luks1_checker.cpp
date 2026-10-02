// Digest-based LUKS1 CPU reference adapter; never mounts or writes a target.
// Supported profile: AES-256 / CBC-ESSIV:SHA256 / PBKDF2-HMAC-SHA1.
// Specification and independent implementation references: LUKS1_CHECKER.md.
#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <memory>
#include <signal.h>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>
#include <openssl/crypto.h>
#include <openssl/evp.h>

namespace {
using Bytes = std::vector<unsigned char>;
constexpr size_t FILE_CAP = 64 * 1024 * 1024;
constexpr uint32_t ACTIVE = 0xac71f3, INACTIVE = 0xdead, ROUND_CAP = 10000000;
[[noreturn]] void fail(const std::string &s) { throw std::runtime_error(s); }
void need(bool ok, const std::string &s) { if (!ok) fail(s); }
uint64_t number(const std::string &s) {
  need(!s.empty() && s.size() <= 20, "invalid count");
  uint64_t n = 0;
  for (unsigned char c : s) {
    need(c >= '0' && c <= '9' && n <= (UINT64_MAX - (c - '0')) / 10, "invalid count");
    n = n * 10 + c - '0';
  }
  return n;
}
bool identity(const std::string &s, size_t n) {
  return s.size() == n && std::all_of(s.begin(), s.end(), [](char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
  });
}
std::string hex(const unsigned char *p, size_t n) {
  constexpr char digits[] = "0123456789abcdef";
  std::string result; result.reserve(n * 2);
  for (size_t i = 0; i < n; ++i) { result += digits[p[i] >> 4]; result += digits[p[i] & 15]; }
  return result;
}
struct FD {
  int n;
  explicit FD(int value) : n(value) { need(n >= 0, "cannot open descriptor"); }
  ~FD() { close(n); }
  FD(const FD &) = delete;
  FD &operator=(const FD &) = delete;
};
Bytes read_file(const std::string &path, size_t cap = FILE_CAP) {
  FD fd(open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
  struct stat st{};
  need(fstat(fd.n, &st) == 0 && S_ISREG(st.st_mode) && st.st_size >= 0 && uint64_t(st.st_size) <= cap,
       "target must be a bounded regular file, not a device");
  Bytes data(size_t(st.st_size)); size_t pos = 0;
  while (pos < data.size()) {
    auto n = read(fd.n, data.data() + pos, data.size() - pos);
    if (n < 0 && errno == EINTR) continue;
    need(n > 0, "short file read"); pos += size_t(n);
  }
  unsigned char extra;
  need(read(fd.n, &extra, 1) == 0, "file changed while reading");
  return data;
}
void digest(const EVP_MD *md, const unsigned char *p, size_t n, unsigned char *out, unsigned expected) {
  unsigned size = 0;
  need(EVP_Digest(p, n, out, &size, md, nullptr) == 1 && size == expected, "digest operation failed");
}
std::string sha(const Bytes &data) {
  std::array<unsigned char, 32> out{};
  digest(EVP_sha256(), data.data(), data.size(), out.data(), out.size());
  return hex(out.data(), out.size());
}
uint32_t be32(const Bytes &d, size_t p) {
  need(p + 4 <= d.size(), "truncated header field");
  return uint32_t(d[p]) << 24 | uint32_t(d[p+1]) << 16 | uint32_t(d[p+2]) << 8 | d[p+3];
}
std::string field(const Bytes &d, size_t pos, size_t len) {
  need(pos + len <= d.size(), "truncated header string");
  const auto end = std::find(d.begin() + pos, d.begin() + pos + len, 0);
  need(end != d.begin() + pos + len, "unterminated header string");
  return std::string(d.begin() + pos, end);
}
struct SecretBytes {
  Bytes value;
  explicit SecretBytes(size_t n) : value(n) {}
  ~SecretBytes() { OPENSSL_cleanse(value.data(), value.size()); }
};
struct Cipher {
  EVP_CIPHER_CTX *p = EVP_CIPHER_CTX_new();
  Cipher() { need(p != nullptr, "cipher allocation failed"); }
  ~Cipher() { EVP_CIPHER_CTX_free(p); }
  Cipher(const Cipher &) = delete;
};
struct Slot { uint32_t index, iterations, offset, stripes; size_t bytes; };
struct Header {
  Bytes data;
  std::string hash;
  uint32_t payload, rounds;
  std::vector<Slot> slots;
  explicit Header(const std::string &path) : data(read_file(path)), hash(sha(data)) {
    need(data.size() >= 592 && std::memcmp(data.data(), "LUKS\xba\xbe", 6) == 0 &&
         data[6] == 0 && data[7] == 1, "not a complete LUKS1 header");
    need(field(data, 8, 32) == "aes" && field(data, 40, 32) == "cbc-essiv:sha256" &&
         field(data, 72, 32) == "sha1" && be32(data, 108) == 32, "unsupported LUKS1 profile");
    payload = be32(data, 104); rounds = be32(data, 164);
    need(rounds > 0 && rounds <= ROUND_CAP, "unsupported master-key digest iteration count");
    for (uint32_t i = 0; i < 8; ++i) {
      size_t p = 208 + i * 48; auto active = be32(data, p);
      need(active == ACTIVE || active == INACTIVE, "invalid keyslot marker");
      if (active == INACTIVE) continue; // Retained disabled metadata is not an active keyslot.
      Slot s{i, be32(data, p+4), be32(data, p+40), be32(data, p+44), 0};
      need(s.iterations > 0 && s.iterations <= ROUND_CAP && s.stripes > 0 && s.stripes <= 524288,
           "unsupported active keyslot parameters");
      s.bytes = (size_t(s.stripes) * 32 + 511) / 512 * 512;
      uint64_t begin = uint64_t(s.offset) * 512, end = begin + s.bytes;
      need(begin >= 1024 && end <= data.size(), "active key material missing or overlaps header");
      need(payload == 0 || end <= uint64_t(payload) * 512, "key material overlaps payload");
      for (const auto &old : slots) {
        uint64_t a = uint64_t(old.offset) * 512, b = a + old.bytes;
        need(end <= a || b <= begin, "overlapping active keyslots");
      }
      slots.push_back(s);
    }
    need(!slots.empty(), "no active keyslots; no password checks performed");
  }
  std::vector<Slot> select(const std::string &name) const {
    if (name == "all") return slots;
    uint64_t index = number(name); need(index < 8, "invalid slot number");
    for (const auto &s : slots) if (s.index == index) return {s};
    fail("requested keyslot is disabled");
  }
  bool check(const std::string &password, const Slot &s) const {
    SecretBytes key(32), essiv(32), plain(s.bytes), master(32), actual(20);
    size_t meta = 208 + s.index * 48;
    need(PKCS5_PBKDF2_HMAC(password.data(), int(password.size()), data.data()+meta+8, 32,
                          int(s.iterations), EVP_sha1(), 32, key.value.data()) == 1, "slot PBKDF2 failed");
    digest(EVP_sha256(), key.value.data(), 32, essiv.value.data(), 32);
    Cipher iv_cipher, cbc;
    need(EVP_EncryptInit_ex(iv_cipher.p, EVP_aes_256_ecb(), nullptr, essiv.value.data(), nullptr) == 1 &&
         EVP_CIPHER_CTX_set_padding(iv_cipher.p, 0) == 1, "ESSIV initialization failed");
    for (size_t sector = 0; sector < s.bytes / 512; ++sector) {
      std::array<unsigned char, 16> number_bytes{}, iv{};
      for (unsigned k = 0; k < 8; ++k) number_bytes[k] = (uint64_t(sector) >> (k*8)) & 255;
      int written = 0, final = 0;
      need(EVP_EncryptUpdate(iv_cipher.p, iv.data(), &written, number_bytes.data(), 16) == 1 && written == 16,
           "ESSIV encryption failed");
      need(EVP_DecryptInit_ex(cbc.p, EVP_aes_256_cbc(), nullptr, key.value.data(), iv.data()) == 1 &&
           EVP_CIPHER_CTX_set_padding(cbc.p, 0) == 1, "CBC initialization failed");
      auto *out = plain.value.data() + sector * 512;
      need(EVP_DecryptUpdate(cbc.p, out, &written, data.data()+size_t(s.offset)*512+sector*512, 512) == 1 &&
           written == 512 && EVP_DecryptFinal_ex(cbc.p, out+written, &final) == 1 && final == 0,
           "key material decryption failed");
    }
    for (uint32_t stripe = 0; stripe < s.stripes; ++stripe) {
      for (size_t k = 0; k < 32; ++k) master.value[k] ^= plain.value[size_t(stripe)*32+k];
      if (stripe + 1 == s.stripes) break;
      // LUKS1 H1 diffusion: SHA1(BE32(block) || that block), truncate last digest.
      for (size_t pos = 0, block = 0; pos < 32; pos += 20, ++block) {
        std::array<unsigned char, 24> input{}; std::array<unsigned char, 20> hashed{};
        input[3] = static_cast<unsigned char>(block);
        size_t n = std::min(size_t(20), size_t(32)-pos);
        std::copy_n(master.value.data()+pos, n, input.data()+4);
        digest(EVP_sha1(), input.data(), n+4, hashed.data(), 20);
        std::copy_n(hashed.data(), n, master.value.data()+pos);
        OPENSSL_cleanse(input.data(), input.size()); OPENSSL_cleanse(hashed.data(), hashed.size());
      }
    }
    need(PKCS5_PBKDF2_HMAC(reinterpret_cast<const char *>(master.value.data()), 32, data.data()+132, 32,
                          int(rounds), EVP_sha1(), 20, actual.value.data()) == 1, "master-key PBKDF2 failed");
    return CRYPTO_memcmp(actual.value.data(), data.data()+112, 20) == 0;
  }
  void describe() const {
    std::cout << "{\"schema\":\"luks1-digest-profile-v1\",\"target_sha256\":\"" << hash
      << "\",\"cipher\":\"aes\",\"mode\":\"cbc-essiv:sha256\",\"hash\":\"sha1\",\"key_bytes\":32"
      << ",\"payload_sector\":" << payload << ",\"digest_iterations\":" << rounds << ",\"active_slots\":[";
    bool first = true;
    for (const auto &s : slots) {
      if (!first) std::cout << ','; first = false;
      std::cout << "{\"slot\":" << s.index << ",\"iterations\":" << s.iterations << ",\"offset_sector\":"
        << s.offset << ",\"stripes\":" << s.stripes << '}';
    }
    std::cout << "],\"verification\":\"stored-master-key-digest\",\"backend\":\"CPU-reference\"}\n";
  }
};
template<class F> void frames(uint64_t expected, F fn) {
  std::array<unsigned char, 65536> buffer{}; std::string value; value.reserve(128);
  int remaining = -1; uint64_t count = 0;
  for (;;) {
    auto n = read(STDIN_FILENO, buffer.data(), buffer.size());
    if (n < 0 && errno == EINTR) continue;
    need(n >= 0, "candidate input failed"); if (n == 0) break;
    size_t pos = 0;
    while (pos < size_t(n)) {
      if (remaining < 0) {
        remaining = buffer[pos++];
        need(remaining <= 128 && count < expected, "excess or invalid candidate frame");
      }
      size_t take = std::min(size_t(remaining), size_t(n)-pos);
      value.append(reinterpret_cast<const char *>(buffer.data()+pos), take); pos += take; remaining -= int(take);
      if (remaining == 0) {
        fn(value, count++); OPENSSL_cleanse(value.data(), value.size()); value.clear(); remaining = -1;
      }
    }
  }
  need(remaining == -1 && count == expected, "truncated candidate stream; no receipt issued");
}
std::string escape_option(const std::string &s) {
  std::string result; for (char c : s) { result += c; if (c == ',') result += ','; } return result;
}
struct AnonymousFile {
  std::unique_ptr<FILE, decltype(&fclose)> f{tmpfile(), fclose};
  FD fd;
  AnonymousFile() : fd(f ? fcntl(fileno(f.get()), F_DUPFD_CLOEXEC, 10) : -1) {}
  void set(const std::string &value) {
    size_t p = 0;
    while (p < value.size()) {
      auto n = write(fd.n, value.data()+p, value.size()-p);
      if (n < 0 && errno == EINTR) continue;
      need(n > 0, "anonymous secret write failed"); p += size_t(n);
    }
    need(lseek(fd.n, 0, SEEK_SET) == 0, "anonymous secret rewind failed");
  }
};
struct PrivateOutput {
  std::string directory, file;
  PrivateOutput() {
    std::array<char, 32> path{};
    const char pattern[] = "/tmp/luks1-confirm-XXXXXX";
    std::copy_n(pattern, sizeof(pattern), path.data());
    need(mkdtemp(path.data()) != nullptr, "private confirmation directory failed");
    directory = path.data(); file = directory + "/sector";
  }
  ~PrivateOutput() { unlink(file.c_str()); rmdir(directory.c_str()); }
};
void confirm_qemu(const std::string &exe, const std::string &exe_hash, const std::string &target,
                  const std::string &target_hash) {
  need(identity(exe_hash, 64) && identity(target_hash, 64), "invalid confirmation identity");
  need(sha(read_file(exe, 256*1024*1024)) == exe_hash, "QEMU executable changed");
  Header header(target); need(header.hash == target_hash, "confirmation target changed");
  std::string password;
  frames(1, [&](const std::string &v, uint64_t) { password = v; });
  // QEMU uses strlen(password); never silently truncate a binary candidate.
  need(password.find('\0') == std::string::npos, "QEMU confirmation does not support embedded NUL");
  AnonymousFile secret, diagnostics;
  PrivateOutput output;
  secret.set(password); OPENSSL_cleanse(password.data(), password.size());
  std::vector<std::string> args{exe, "dd", "--object", "secret,id=verify,format=raw,file=/dev/fd/3",
    "--image-opts", "bs=512", "count=1",
    "if=driver=luks,key-secret=verify,file.driver=file,file.filename=" + escape_option(target), "of="+output.file};
  std::vector<char *> argv; for (auto &s : args) argv.push_back(s.data()); argv.push_back(nullptr);
  pid_t pid = fork(); need(pid >= 0, "QEMU fork failed");
  if (pid == 0) {
    umask(0077);
    if (dup2(secret.fd.n, 3) < 0 ||
        dup2(diagnostics.fd.n, STDOUT_FILENO) < 0 || dup2(diagnostics.fd.n, STDERR_FILENO) < 0) _exit(126);
    execv(exe.c_str(), argv.data()); _exit(127);
  }
  int status = 0; bool done = false;
  auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
  while (std::chrono::steady_clock::now() < deadline) {
    auto result = waitpid(pid, &status, WNOHANG);
    if (result == pid) { done = true; break; }
    if (result < 0 && errno != EINTR) { kill(pid, SIGKILL); waitpid(pid, &status, 0); fail("QEMU wait failed"); }
    usleep(10000);
  }
  if (!done) { kill(pid, SIGKILL); waitpid(pid, &status, 0); fail("QEMU confirmation timeout"); }
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    std::array<char, 1024> message{};
    auto n = pread(diagnostics.fd.n, message.data(), message.size(), 0);
    fail("independent QEMU unlock/read failed; no confirmation issued: " +
         (n > 0 ? std::string(message.data(), size_t(n)) : "no diagnostic"));
  }
  need(read_file(output.file, 512).size() == 512, "QEMU did not read one plaintext sector");
  need(sha(read_file(target)) == target_hash && sha(read_file(exe, 256*1024*1024)) == exe_hash,
       "confirmation input changed during verification");
  std::cout << "{\"schema\":\"confirmation-v1\",\"target\":\"" << target_hash << "\",\"confirmed\":true}\n";
}
} // namespace
int main(int argc, char **argv) {
  std::ios::sync_with_stdio(false);
  try {
    if (argc == 3 && std::string(argv[1]) == "inspect") { Header(argv[2]).describe(); return 0; }
    if (argc == 6 && std::string(argv[1]) == "confirm-qemu") {
      confirm_qemu(argv[2], argv[3], argv[4], argv[5]); return 0;
    }
    need(argc == 10 && std::string(argv[1]) == "check",
         "usage: inspect HEADER | check control|recovery all|SLOT all|LIMIT HEADER JOB PLAN_SHA TARGET_SHA COUNT | confirm-qemu QEMU QEMU_SHA HEADER TARGET_SHA");
    std::string purpose = argv[2], selected = argv[3], limit_text = argv[4];
    need(purpose == "control" || purpose == "recovery", "explicit control/recovery purpose required");
    uint64_t limit = limit_text == "all" ? UINT64_MAX : number(limit_text), count = number(argv[9]);
    need(count > 0 && count <= 100000000, "invalid submitted count");
    std::string job = argv[6], plan = argv[7], target_hash = argv[8];
    need(identity(job, 32) && identity(plan, 64) && identity(target_hash, 64), "invalid assignment identity");
    Header header(argv[5]); need(header.hash == target_hash, "target SHA256 mismatch");
    auto slots = header.select(selected);
    uint64_t negatives = 0; bool hit = false; std::string found;
    frames(count, [&](const std::string &candidate, uint64_t position) {
      if (hit || position >= limit) return; // Drain and validate, without crediting the tail.
      for (const auto &slot : slots) if (header.check(candidate, slot)) { hit = true; found = candidate; break; }
      if (!hit) ++negatives;
    });
    need(sha(read_file(argv[5])) == target_hash, "target changed during checking");
    std::cout << "{\"schema\":\"checked-prefix-v1\",\"job\":\"" << job << "\",\"plan\":\"" << plan
      << "\",\"target\":\"" << target_hash << "\",\"submitted\":" << count << ",\"negative_prefix\":" << negatives
      << ",\"status\":\"" << (hit ? "hit" : negatives == count ? "negative" : "partial") << '"';
    if (hit) std::cout << ",\"hit_hex\":\"" << hex(reinterpret_cast<const unsigned char *>(found.data()), found.size()) << '"';
    std::cout << "}\n";
    OPENSSL_cleanse(found.data(), found.size());
    return 0;
  } catch (const std::exception &e) { std::cerr << "ERROR: " << e.what() << '\n'; return 2; }
}
