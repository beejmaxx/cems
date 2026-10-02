// TEST-ONLY stream oracle. No target/password, plan parser, coverage writes,
// probability compiler, hashes, or seen-candidate table are used here.
#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

using U64 = uint64_t;
[[noreturn]] void fail(const std::string &message) { throw std::runtime_error(message); }
U64 number(const std::string &s) {
  if(s.empty()||s.size()>20) fail("invalid decimal integer");
  U64 n=0;
  for(unsigned char c:s) {
    if(c<'0'||c>'9'||n>(UINT64_MAX-(c-'0'))/10) fail("integer overflow");
    n=n*10+c-'0';
  }
  return n;
}
std::string token(std::istream &f) { std::string s; if(!(f>>s)) fail("truncated oracle"); return s; }
unsigned nibble(char c) {
  if(c>='0'&&c<='9') return unsigned(c-'0');
  if(c>='a'&&c<='f') return unsigned(c-'a'+10);
  fail("noncanonical hex");
}
std::string unhex(const std::string &s) {
  if(s=="-") return {};
  if(s.empty()||s.size()>256||s.size()%2) fail("invalid text encoding");
  std::string out;
  for(size_t i=0;i<s.size();i+=2) {
    char c=char(nibble(s[i])*16+nibble(s[i+1]));
    if(c>='0'&&c<='9') fail("oracle text contains digits");
    out.push_back(c);
  }
  return out;
}
struct Segment { std::string before; U64 lo,hi,end; std::vector<std::string> afters; };
struct Index {
  unsigned digits=0;
  U64 count=0;
  std::vector<Segment> segments;
  explicit Index(const std::string &path) {
    if(std::filesystem::file_size(path)>16*1024*1024) fail("oracle file cap");
    std::ifstream f(path); if(!f||token(f)!="RECIPE1") fail("oracle schema");
    U64 width=number(token(f)), size=number(token(f)), declared=number(token(f));
    if(width<1||width>12||size>200000) fail("oracle resource bounds");
    digits=unsigned(width);
    U64 bound=1; for(unsigned i=0;i<digits;++i) bound*=10;
    for(U64 i=0;i<size;++i) {
      Segment s; s.before=unhex(token(f)); s.lo=number(token(f)); s.hi=number(token(f));
      U64 suffixes=number(token(f));
      if(s.lo>=s.hi||s.hi>bound||!suffixes||suffixes>4096) fail("invalid recipe interval");
      for(U64 j=0;j<suffixes;++j) {
        std::string after=unhex(token(f));
        if(s.before.size()+digits+after.size()>128||(!s.afters.empty()&&s.afters.back()>=after)) fail("suffix ordering/length");
        s.afters.push_back(std::move(after));
      }
      __uint128_t next=__uint128_t(count)+__uint128_t(s.hi-s.lo)*suffixes;
      if(next>UINT64_MAX) fail("oracle count overflow");
      count=U64(next); s.end=count; segments.push_back(std::move(s));
    }
    std::string extra; if(f>>extra||!f.eof()||count!=declared) fail("oracle count/trailing data");
  }
};
struct Cursor {
  const Index &index;
  size_t segment=0,suffix=0;
  U64 value=0;
  Cursor(const Index &idx,U64 rank):index(idx) {
    if(rank>index.count) fail("start outside oracle");
    if(rank==index.count) {segment=index.segments.size(); return;}
    segment=size_t(std::upper_bound(index.segments.begin(),index.segments.end(),rank,
      [](U64 r,const Segment &s){return r<s.end;})-index.segments.begin());
    const auto &s=index.segments.at(segment);
    U64 local=rank-(segment?index.segments[segment-1].end:0);
    value=s.lo+local/s.afters.size(); suffix=size_t(local%s.afters.size());
  }
  void compare(const char *bytes,size_t size,U64 position) {
    if(segment>=index.segments.size()) fail("stream exceeded oracle");
    const auto &s=index.segments[segment]; const auto &after=s.afters[suffix];
    std::array<char,128> expected{};
    size_t length=s.before.size()+index.digits+after.size();
    std::memcpy(expected.data(),s.before.data(),s.before.size());
    U64 v=value;
    for(unsigned i=0;i<index.digits;++i) {
      expected[s.before.size()+index.digits-1-i]=char('0'+v%10); v/=10;
    }
    std::memcpy(expected.data()+s.before.size()+index.digits,after.data(),after.size());
    if(size!=length||std::memcmp(bytes,expected.data(),length)!=0)
      fail("candidate/order mismatch at stream offset "+std::to_string(position));
    if(++suffix==s.afters.size()) {
      suffix=0;
      if(++value==s.hi) {
        ++segment;
        if(segment<index.segments.size()) value=index.segments[segment].lo;
      }
    }
  }
};
int main(int argc,char **argv) {
  std::ios::sync_with_stdio(false);
  try {
    if(argc!=4) fail("usage: recipe-audit oracle.recipes start count < framed-candidates");
    Index index(argv[1]); U64 start=number(argv[2]),wanted=number(argv[3]);
    if(start>index.count||wanted>index.count-start||wanted>1000000000) fail("audit range outside oracle");
    Cursor cursor(index,start);
    auto began=std::chrono::steady_clock::now();
    std::array<char,65536> buffer{}; std::array<char,128> frame{};
    size_t have=0; int length=-1; U64 frames=0;
    for(;;) {
      auto n=read(STDIN_FILENO,buffer.data(),buffer.size());
      if(n<0) {if(errno==EINTR) continue; fail("input read failed");}
      if(!n) break;
      size_t position=0;
      while(position<size_t(n)) {
        if(length<0) {
          length=uint8_t(buffer[position++]); have=0;
          if(length>128||frames>=wanted) fail("excess/invalid frame");
        }
        size_t take=std::min(size_t(length)-have,size_t(n)-position);
        std::memcpy(frame.data()+have,buffer.data()+position,take); have+=take; position+=take;
        if(have==size_t(length)) {cursor.compare(frame.data(),have,frames++); length=-1;}
      }
    }
    if(length!=-1||frames!=wanted) fail("truncated stream");
    double seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-began).count();
    std::cout<<"{\"verified\":true,\"frames_compared_exactly\":"<<frames<<",\"seconds\":"<<seconds
             <<",\"password_checks\":0,\"method\":\"independent recipe arithmetic, full byte comparison\"}\n";
    return 0;
  } catch(const std::exception &e) {std::cerr<<"ERROR: "<<e.what()<<"\n"; return 2;}
}
