// CPU-only generic-checker protocol fixture. No cryptography or real target.
#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <unistd.h>

[[noreturn]] void error(const std::string &s) { throw std::runtime_error(s); }
uint64_t integer(const std::string &s) {
  if(s.empty()||s.size()>20) error("invalid count");
  uint64_t result=0;
  for(unsigned char c:s) {
    if(c<'0'||c>'9'||result>(UINT64_MAX-(c-'0'))/10) error("invalid count");
    result=result*10+c-'0';
  }
  return result;
}
bool digest(const std::string &s,size_t length) {
  return s.size()==length&&std::all_of(s.begin(),s.end(),[](char c){return (c>='0'&&c<='9')||(c>='a'&&c<='f');});
}
std::string target(const std::string &path) {
  std::ifstream file(path,std::ios::binary); if(!file) error("target unavailable");
  std::string result; char c;
  while(file.get(c)) { if(result.size()==128) error("synthetic target too long"); result.push_back(c); }
  if(!file.eof()) error("target read failed"); return result;
}
std::string hex(const std::string &s) {
  constexpr char digits[]="0123456789abcdef"; std::string result;
  for(unsigned char c:s) {result.push_back(digits[c>>4]); result.push_back(digits[c&15]);} return result;
}
template<class F> uint64_t frames(uint64_t expected,F fn) {
  std::array<char,65536> buffer{}; std::string value; value.reserve(128);
  int remaining=-1; uint64_t count=0;
  for(;;) {
    auto bytes=read(STDIN_FILENO,buffer.data(),buffer.size());
    if(bytes<0) {if(errno==EINTR) continue; error("read failed");}
    if(!bytes) break;
    size_t pos=0;
    while(pos<size_t(bytes)) {
      if(remaining<0) {
        remaining=uint8_t(buffer[pos++]);
        if(remaining>128||count>=expected) error("excess/invalid frame");
      }
      auto n=std::min(size_t(remaining),size_t(bytes)-pos);
      value.append(buffer.data()+pos,n); pos+=n; remaining-=int(n);
      if(!remaining) {fn(value,count++); value.clear(); remaining=-1;}
    }
  }
  if(remaining!=-1||count!=expected) error("truncated stream"); return count;
}
int main(int argc,char **argv) {
  std::ios::sync_with_stdio(false);
  try {
    if(argc==4&&std::string(argv[1])=="confirm") {
      auto secret=target(argv[2]); std::string context=argv[3]; if(!digest(context,64)) error("invalid target identity");
      bool match=false; frames(1,[&](const std::string &value,uint64_t){match=value==secret;});
      std::cout<<"{\"schema\":\"confirmation-v1\",\"target\":\""<<context<<"\",\"confirmed\":"<<(match?"true":"false")<<"}\n";
    } else if(argc==9&&std::string(argv[1])=="check") {
      uint64_t limit=std::string(argv[2])=="all"?UINT64_MAX:integer(argv[2]);
      std::string mode=argv[3],secret=target(argv[4]),job=argv[5],plan=argv[6],context=argv[7];
      if(!digest(job,32)||!digest(plan,64)||!digest(context,64)) error("invalid assignment identity");
      uint64_t count=integer(argv[8]),negative=0; bool hit=false; std::string found,first;
      frames(count,[&](const std::string &value,uint64_t position) {
        if(!position) first=value;
        if(hit||position>=limit) return; // Drain, but do NOT evaluate or credit the tail.
        if(value==secret) {hit=true; found=value;} else ++negative;
      });
      if(mode=="exit") return 3;
      if(mode=="malformed") {std::cout<<"not-json\n"; return 0;}
      if(mode=="block") {for(;;) pause();}
      if(mode=="spam") {std::cout<<std::string(100000,'x'); return 0;}
      if(mode=="wrong-job") job=std::string(32,'0');
      if(mode=="overclaim") negative=count+1;
      if(mode=="false-hit") {negative=0; hit=true; found="not-the-delivered-candidate";}
      if(mode=="lie-hit") {negative=0; hit=true; found=first;}
      if(mode!="ok"&&mode!="wrong-job"&&mode!="overclaim"&&mode!="false-hit"&&mode!="lie-hit") error("unknown fixture mode");
      auto status=hit?"hit":(negative==count?"negative":"partial");
      std::cout<<"{\"schema\":\"checked-prefix-v1\",\"job\":\""<<job<<"\",\"plan\":\""<<plan
               <<"\",\"target\":\""<<context<<"\",\"submitted\":"<<count<<",\"negative_prefix\":"<<negative
               <<",\"status\":\""<<status<<"\"";
      if(hit) std::cout<<",\"hit_hex\":\""<<hex(found)<<"\"";
      std::cout<<"}\n";
    } else error("usage: check limit mode target job plan target_sha count | confirm target target_sha");
    return 0;
  } catch(const std::exception &e) {std::cerr<<"ERROR: "<<e.what()<<"\n"; return 2;}
}
