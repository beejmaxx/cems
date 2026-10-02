// Checked native revision. core.cpp and build/prep remain frozen for pinned v1
// campaigns. The enumeration, scoring and PLAB0002 representation are unchanged.
// Reader hardening: prove that probability-band languages are pairwise disjoint.
// Standalone bounded preparation experiment; no dependency on recovery/*.
#include <algorithm>
#include <array>
#include <chrono>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
#include <fcntl.h>
#include <sys/resource.h>
#include <unistd.h>

using U = __uint128_t;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
constexpr U UMAX = ~U(0);
constexpr uint64_t FILE_CAP = 64ull << 20;
constexpr uint64_t RESERVE = 10ull << 30;
// Offline experiment budgets. The initial 1M histogram-entry cap stopped at
// ~65 MiB RSS; this measured follow-up admits more preprocessing, still under
// the independent 768 MiB RSS / 90 second / 64 MiB output guards.
constexpr size_t NODE_CAP = 600000, EDGE_CAP = 8000000;
constexpr size_t STATE_CAP = 400000, BAND_CAP = 8000000;

[[noreturn]] void fail(const std::string &s) { throw std::runtime_error(s); }
U add(U a, U b) { if (a > UMAX-b) fail("uint128 addition limit"); return a+b; }
U mul(U a, U b) { if (b && a > UMAX/b) fail("uint128 multiplication limit"); return a*b; }
uint64_t count_add(uint64_t a, uint64_t b) {
  if (a > UINT64_MAX-b) fail("uint64 count limit"); return a+b;
}
U gcd(U a, U b) { while (b) { U r=a%b; a=b; b=r; } return a; }
std::string dec(U n) {
  std::string s; do { s.push_back(char('0'+n%10)); n/=10; } while(n);
  std::reverse(s.begin(), s.end()); return s;
}
U number(const std::string &s) {
  if (s.empty() || s.size()>39) fail("invalid unsigned number");
  U n=0; for (unsigned char c:s) { if(c<'0'||c>'9') fail("invalid unsigned number"); n=add(mul(n,10),c-'0'); }
  return n;
}
uint64_t u64(const std::string &s) { U n=number(s); if(n>UINT64_MAX) fail("uint64 limit"); return uint64_t(n); }
U read_u(std::istream &in) { std::string s; if(!(in>>s)) fail("truncated input"); return number(s); }
size_t bounded(std::istream &in, size_t cap) { U n=read_u(in); if(n>cap) fail("input count limit"); return size_t(n); }
uint64_t rss() {
  struct rusage r{}; if(getrusage(RUSAGE_SELF,&r)) fail("getrusage failed");
#ifdef __APPLE__
  return uint64_t(r.ru_maxrss);
#else
  return uint64_t(r.ru_maxrss)*1024;
#endif
}
struct Guard {
  Clock::time_point start=Clock::now(); uint64_t operations=0;
  void tick() {
    if((++operations & 1023)==0) {
      if(std::chrono::duration<double>(Clock::now()-start).count()>90) fail("90 second operation limit");
      if(rss()>768ull*1024*1024) fail("768 MiB RSS limit");
    }
  }
};
Guard guard;

struct Edge { uint8_t byte; uint32_t child; bool operator==(const Edge &) const = default; };
struct Node { bool final; std::vector<Edge> edges; uint64_t count; unsigned depth; };
struct Key { bool final; std::vector<Edge> edges; bool operator==(const Key &) const = default; };
struct KeyHash {
  size_t operator()(const Key &k) const {
    size_t h=k.final; for(auto e:k.edges) h=(h*1000003)^((size_t(e.child)<<8)|e.byte); return h;
  }
};
struct SliceKey { uint32_t node; uint64_t start,count; bool operator==(const SliceKey &) const = default; };
struct SliceHash { size_t operator()(const SliceKey &k) const { return (k.node*1000003ull+k.start)*1000003ull+k.count; } };
struct Pool {
  std::vector<Node> nodes;
  std::unordered_map<Key,uint32_t,KeyHash> canonical;
  std::unordered_map<uint64_t,uint32_t> unions, differences;
  std::unordered_map<SliceKey,uint32_t,SliceHash> slices;
  size_t edge_count=0;
  Pool() { intern(false,{}); intern(true,{}); }
  uint32_t intern(bool terminal, std::vector<Edge> edges) {
    guard.tick();
    edges.erase(std::remove_if(edges.begin(),edges.end(),[](Edge e){ return e.child==0; }),edges.end());
    Key key{terminal,edges}; if(auto it=canonical.find(key);it!=canonical.end()) return it->second;
    if(nodes.size()>=NODE_CAP || edge_count+edges.size()>EDGE_CAP) fail("language DAG limit");
    uint64_t count=terminal; unsigned depth=0; int previous=-1;
    for(auto e:edges) {
      if(e.byte<=previous || e.child>=nodes.size()) fail("non-deterministic/non-topological edge");
      previous=e.byte; count=count_add(count,nodes[e.child].count);
      depth=std::max(depth,nodes[e.child].depth+1);
    }
    if(depth>128) fail("128 byte length limit");
    uint32_t id=uint32_t(nodes.size()); edge_count+=edges.size();
    nodes.push_back({terminal,std::move(edges),count,depth}); canonical.emplace(std::move(key),id); return id;
  }
  uint32_t combine(uint32_t a,uint32_t b,bool difference) {
    guard.tick();
    if(!a || a==b) return difference?0:a;
    if(!b) return a;
    if(!difference && a>b) std::swap(a,b);
    uint64_t key=(uint64_t(a)<<32)|b;
    auto &memo=difference?differences:unions;
    if(auto it=memo.find(key);it!=memo.end()) return it->second;
    if(memo.size()>=STATE_CAP*4) fail("set-operation state limit");
    // Copies are intentional: recursive interning can reallocate nodes.
    Node x=nodes[a],y=nodes[b]; std::vector<Edge> edges;
    size_t j=0;
    for(auto e:x.edges) {
      while(j<y.edges.size() && y.edges[j].byte<e.byte) { if(!difference) edges.push_back(y.edges[j]); ++j; }
      if(j<y.edges.size() && y.edges[j].byte==e.byte) {
        uint32_t child=combine(e.child,y.edges[j++].child,difference);
        if(child) edges.push_back({e.byte,child});
      } else edges.push_back(e);
    }
    if(!difference) while(j<y.edges.size()) edges.push_back(y.edges[j++]);
    uint32_t result=intern(difference?(x.final&&!y.final):(x.final||y.final),std::move(edges));
    memo.emplace(key,result); return result;
  }
  uint32_t unite(uint32_t a,uint32_t b) { if(!a) return b; return combine(a,b,false); }
  uint32_t subtract(uint32_t a,uint32_t b) { return combine(a,b,true); }
  uint32_t slice(uint32_t id,uint64_t start,uint64_t count) {
    guard.tick(); auto total=nodes[id].count;
    if(start>total || count>total-start) fail("slice outside language");
    if(!count) return 0;
    if(!start && count==total) return id;
    SliceKey key{id,start,count}; if(auto it=slices.find(key);it!=slices.end()) return it->second;
    if(slices.size()>=STATE_CAP*4) fail("slice state limit");
    Node node=nodes[id]; bool final=false; std::vector<Edge> edges;
    if(node.final) { if(start) --start; else { final=true; --count; } }
    for(auto e:node.edges) {
      if(!count) break;
      uint64_t n=nodes[e.child].count;
      if(start>=n) { start-=n; continue; }
      auto take=std::min(count,n-start); edges.push_back({e.byte,slice(e.child,start,take)});
      count-=take; start=0;
    }
    uint32_t result=intern(final,std::move(edges)); slices.emplace(key,result); return result;
  }
};
struct Band { U score; uint32_t root; };
struct Plan { U denominator; std::vector<Band> bands; bool complete=true; U next_score=0; };
uint64_t total(const Pool &pool,const Plan &plan) {
  uint64_t n=0; for(auto b:plan.bands) n=count_add(n,pool.nodes[b.root].count); return n;
}
uint32_t selection(Pool &pool,const Plan &plan,uint64_t start,uint64_t count) {
  uint64_t n=total(pool,plan); if(start>n||count>n-start) fail("assignment outside plan");
  uint32_t result=0;
  for(auto b:plan.bands) {
    if(!count) break;
    uint64_t size=pool.nodes[b.root].count;
    if(start>=size) { start-=size; continue; }
    auto take=std::min(count,size-start);
    result=pool.unite(result,pool.slice(b.root,start,take)); count-=take; start=0;
  }
  return result;
}

struct WEdge { uint8_t byte; uint32_t child; U weight; };
struct WNode { U final; std::vector<WEdge> edges; };
struct Part { uint32_t node; U amplitude; bool operator==(const Part &) const = default; };
using State=std::vector<Part>;
struct StateHash {
  size_t operator()(const State &s) const {
    size_t h=0; for(auto p:s) { h=h*1000003+p.node; h=h*1000003+uint64_t(p.amplitude); h=h*1000003+uint64_t(p.amplitude>>64); } return h;
  }
};
struct Source { U denominator; std::vector<WNode> nodes; State roots; };
Source read_source(const fs::path &path) {
  if(fs::file_size(path)>FILE_CAP) fail("input file cap");
  std::ifstream in(path); std::string magic; in>>magic; if(magic!="SWG1") fail("unsupported source version");
  Source s; s.denominator=read_u(in); if(!s.denominator) fail("zero denominator");
  size_t count=bounded(in,100000), roots=bounded(in,64); if(!count||!roots) fail("empty source");
  for(size_t i=0;i<roots;++i) {
    auto node=bounded(in,count-1); U a=read_u(in); if(!a) fail("zero root amplitude"); s.roots.push_back({uint32_t(node),a});
  }
  size_t edges=0;
  for(size_t i=0;i<count;++i) {
    WNode n{read_u(in),{}}; size_t k=bounded(in,256); int last=-1; edges+=k;
    if(edges>EDGE_CAP) fail("source edge cap");
    for(size_t j=0;j<k;++j) {
      size_t byte=bounded(in,255), child=bounded(in,count-1); U weight=read_u(in);
      if(int(byte)<=last||!weight) fail("invalid source edge"); last=int(byte);
      n.edges.push_back({uint8_t(byte),uint32_t(child),weight});
    }
    if(!n.final&&n.edges.empty()) fail("dead source state"); s.nodes.push_back(std::move(n));
  }
  std::string extra; if(in>>extra) fail("trailing source data");
  std::vector<int> status(count); std::vector<U> masses(count); std::vector<unsigned> depths(count);
  std::function<U(uint32_t,unsigned)> visit=[&](uint32_t id,unsigned depth) {
    if(depth>128||status[id]==1) fail("cyclic/overlong source");
    if(status[id]==2) { if(depth+depths[id]>128) fail("overlong source"); return masses[id]; }
    status[id]=1; U mass=s.nodes[id].final; unsigned longest=0;
    for(auto e:s.nodes[id].edges) { mass=add(mass,mul(e.weight,visit(e.child,depth+1))); longest=std::max(longest,depths[e.child]+1); }
    depths[id]=longest; status[id]=2; return masses[id]=mass;
  };
  U mass=0; for(auto root:s.roots) mass=add(mass,mul(root.amplitude,visit(root.node,0)));
  if(mass!=s.denominator) fail("source does not have total probability one");
  return s;
}
struct Compiler {
  Pool &pool; const Source &source;
  struct Built { std::vector<Band> bands; bool complete=true; };
  std::unordered_map<State,Built,StateHash> memo;
  size_t probability_entries=0;
  size_t limit=0;
  std::pair<State,U> normalize(State state) {
    std::sort(state.begin(),state.end(),[](auto a,auto b){return a.node<b.node;});
    State combined; for(auto p:state) {
      if(!combined.empty()&&combined.back().node==p.node) combined.back().amplitude=add(combined.back().amplitude,p.amplitude);
      else combined.push_back(p);
    }
    U factor=0; for(auto p:combined) factor=gcd(factor,p.amplitude);
    if(!factor) fail("empty weighted state");
    for(auto &p:combined) p.amplitude/=factor; return {std::move(combined),factor};
  }
  const Built &compile(const State &state,unsigned depth=0,uint32_t coverage=0) {
    guard.tick(); if(depth>128) fail("compiler depth limit");
    State key=state; key.push_back({UINT32_MAX,coverage});
    if(auto it=memo.find(key);it!=memo.end()) return it->second;
    if(memo.size()>=STATE_CAP) fail("weighted subset state limit");
    U terminal=0; std::map<uint8_t,State> children;
    for(auto p:state) {
      const auto &node=source.nodes[p.node]; terminal=add(terminal,mul(p.amplitude,node.final));
      for(auto e:node.edges) children[e.byte].push_back({e.child,mul(p.amplitude,e.weight)});
    }
    struct Builder { bool final=false; std::vector<Edge> edges; };
    std::map<U,Builder,std::greater<U>> builders;
    bool complete=true;
    std::map<uint8_t,uint32_t> covered_edges;
    for(auto e:pool.nodes[coverage].edges) covered_edges[e.byte]=e.child;
    if(terminal&&!pool.nodes[coverage].final) builders[terminal].final=true;
    for(auto &[byte,parts]:children) {
      auto [child,factor]=normalize(std::move(parts)); const auto &built=compile(child,depth+1,covered_edges[byte]);
      complete=complete&&built.complete;
      for(auto b:built.bands) builders[mul(factor,b.score)].edges.push_back({byte,b.root});
      // Each omitted child score has >= limit+1 strictly higher child scores.
      // Positive scaling preserves this order. It therefore cannot enter the
      // parent's best limit+1 DISTINCT score levels. Ties are kept in full.
      if(limit && builders.size()>limit+1) {
        complete=false;
        while(builders.size()>limit+1) builders.erase(std::prev(builders.end()));
      }
      if(builders.size()+probability_entries>BAND_CAP) fail("probability histogram limit: entries="+std::to_string(probability_entries)+
        " pending="+std::to_string(builders.size())+" states="+std::to_string(memo.size())+
        " language_nodes="+std::to_string(pool.nodes.size())+" rss="+std::to_string(rss()));
    }
    std::vector<Band> bands; bands.reserve(builders.size());
    for(auto &[score,b]:builders) bands.push_back({score,pool.intern(b.final,std::move(b.edges))});
    probability_entries+=bands.size(); if(probability_entries>BAND_CAP) fail("probability histogram limit");
    if(memo.size()>=STATE_CAP) fail("weighted subset state limit");
    return memo.emplace(std::move(key),Built{std::move(bands),complete}).first->second;
  }
  Plan prepare(uint32_t coverage=0) {
    auto [state,factor]=normalize(source.roots); const auto &built=compile(state,0,coverage);
    Plan plan{source.denominator,built.bands,built.complete,0};
    if(limit && plan.bands.size()>limit) {
      plan.next_score=mul(plan.bands[limit].score,factor);
      plan.bands.resize(limit); plan.complete=false;
    }
    U mass=0; for(auto &b:plan.bands) { b.score=mul(b.score,factor); mass=add(mass,mul(b.score,pool.nodes[b.root].count)); }
    if(mass>source.denominator || (plan.complete&&!coverage&&mass!=source.denominator)) fail("compiled probability mass mismatch"); return plan;
  }
};

void append(std::vector<uint8_t> &out,U n,unsigned bytes) {
  if(out.size()+bytes>FILE_CAP) fail("64 MiB plan file limit");
  for(unsigned i=0;i<bytes;++i) { out.push_back(uint8_t(n)); n>>=8; }
  if(n) fail("serialization overflow");
}
void private_publish(const fs::path &path,const std::vector<uint8_t> &data) {
  auto parent=path.has_parent_path()?path.parent_path():fs::path(".");
  if(fs::space(parent).available<RESERVE+data.size()) fail("10 GiB free disk reserve");
  auto temp=path.string()+".part."+std::to_string(getpid());
  int fd=open(temp.c_str(),O_WRONLY|O_CREAT|O_EXCL,0600); if(fd<0) fail("cannot create fresh temporary file");
  try {
    size_t pos=0; while(pos<data.size()) { auto n=write(fd,data.data()+pos,data.size()-pos); if(n<=0) fail("write failed"); pos+=size_t(n); }
    if(fsync(fd)) fail("file fsync failed"); if(close(fd)) { fd=-1; fail("file close failed"); } fd=-1;
    if(link(temp.c_str(),path.c_str())) fail("destination exists or atomic publish failed");
    if(unlink(temp.c_str())) fail("temporary unlink failed");
  } catch(...) { if(fd>=0) close(fd); unlink(temp.c_str()); throw; }
}
uint64_t write_plan(const fs::path &path,const Pool &pool,const Plan &plan) {
  std::vector<uint32_t> ids{0,1}; std::vector<int64_t> mapped(pool.nodes.size(),-1); mapped[0]=0; mapped[1]=1;
  std::function<void(uint32_t)> visit=[&](uint32_t id) {
    if(mapped[id]>=0) return; for(auto e:pool.nodes[id].edges) visit(e.child);
    mapped[id]=int64_t(ids.size()); ids.push_back(id);
  };
  for(auto b:plan.bands) visit(b.root);
  std::vector<uint8_t> raw={'P','L','A','B','0','0','0','2'};
  append(raw,plan.denominator,16); append(raw,ids.size(),4); append(raw,plan.bands.size(),4);
  append(raw,plan.complete,1); append(raw,plan.next_score,16);
  for(auto id:ids) {
    const auto &n=pool.nodes[id]; append(raw,n.final,1); append(raw,n.edges.size(),2);
    for(auto e:n.edges) { append(raw,e.byte,1); append(raw,mapped[e.child],4); }
  }
  for(auto b:plan.bands) { append(raw,b.score,16); append(raw,mapped[b.root],4); }
  private_publish(path,raw); return raw.size();
}
struct Reader {
  std::vector<uint8_t> raw; size_t pos=8;
  explicit Reader(const fs::path &path) {
    if(fs::file_size(path)>FILE_CAP) fail("64 MiB plan input cap"); std::ifstream in(path,std::ios::binary);
    raw.assign(std::istreambuf_iterator<char>(in),{});
    if(raw.size()<49||std::memcmp(raw.data(),"PLAB0002",8)) fail("invalid plan header");
  }
  U get(unsigned n) {
    if(pos+n>raw.size()) fail("truncated plan"); U x=0;
    for(unsigned i=0;i<n;++i) x|=U(raw[pos++])<<(8*i); return x;
  }
};

struct RootSetHash {
  size_t operator()(const std::vector<uint32_t> &roots) const {
    size_t h=roots.size(); for(auto id:roots) h=h*1000003+id; return h;
  }
};
struct ValidationMetrics {
  uint64_t calls=0,states=0,entries=0;
  double seconds=0;
} validation_metrics;

// Each root is one distinct accepting explanation for the SAME output prefix.
// A terminal in two roots, or a repeated nonempty root, proves an intersection.
// Otherwise advance all roots together on each byte. A singleton is already
// deterministic; successful residual sets can be memoized using full equality.
// We never construct/store the candidate set or the union language. In
// particular this does not consume the Pool's language-node budget.
//
// Worst-case residual-state growth is bounded, NOT assumed away. Exhausting a
// validation budget rejects the input before any output/publication; it never
// silently accepts an unchecked plan.
struct BandDisjointness {
  const Pool &pool;
  std::unordered_set<std::vector<uint32_t>,RootSetHash> proven;
  size_t states=0,entries=0;
  void check(std::vector<uint32_t> roots) {
    guard.tick();
    if(roots.size()<2) return;
    std::sort(roots.begin(),roots.end());
    if(std::adjacent_find(roots.begin(),roots.end())!=roots.end())
      fail("overlapping score-band languages");
    if(proven.contains(roots)) return;
    if(states>=STATE_CAP || roots.size()>BAND_CAP-entries)
      fail("plan disjointness validation limit");
    ++states; entries+=roots.size();
    bool terminal=false;
    std::map<uint8_t,std::vector<uint32_t>> children;
    for(auto id:roots) {
      const auto &node=pool.nodes[id];
      if(node.final) {
        if(terminal) fail("overlapping score-band languages");
        terminal=true;
      }
      for(auto edge:node.edges) { guard.tick(); children[edge.byte].push_back(edge.child); }
    }
    for(auto &[byte,next]:children) { (void)byte; check(std::move(next)); }
    proven.insert(std::move(roots));
  }
};
void validate_bands(const Pool &pool,const Plan &p) {
  auto started=Clock::now();
  std::vector<uint32_t> roots; roots.reserve(p.bands.size());
  for(auto b:p.bands) roots.push_back(b.root);
  BandDisjointness proof{pool,{},0,0};
  proof.check(std::move(roots));
  ++validation_metrics.calls; validation_metrics.states+=proof.states;
  validation_metrics.entries+=proof.entries;
  validation_metrics.seconds+=std::chrono::duration<double>(Clock::now()-started).count();
}
void validation_stats(std::ostream &out) {
  out<<",\"validation_calls\":"<<validation_metrics.calls
     <<",\"validation_states\":"<<validation_metrics.states
     <<",\"validation_entries\":"<<validation_metrics.entries
     <<",\"validation_seconds\":"<<validation_metrics.seconds;
}

Plan read_plan(const fs::path &path,Pool &pool) {
  Reader r(path); Plan p{r.get(16),{}};
  size_t nn=size_t(r.get(4)),nb=size_t(r.get(4));
  if(nn<2||nn>NODE_CAP||nb>BAND_CAP||!p.denominator) fail("plan count/denominator limit");
  auto complete=r.get(1); if(complete>1) fail("invalid plan completeness");
  p.complete=bool(complete); p.next_score=r.get(16);
  std::vector<uint32_t> mapped; mapped.reserve(nn);
  for(size_t i=0;i<nn;++i) {
    auto f=r.get(1); size_t n=size_t(r.get(2)); if(f>1||n>256) fail("invalid plan node");
    std::vector<Edge> edges; int last=-1;
    for(size_t j=0;j<n;++j) {
      auto c=uint8_t(r.get(1)); auto child=uint32_t(r.get(4));
      if(child>=i||c<=last) fail("invalid plan topology"); last=c; edges.push_back({c,mapped[child]});
    }
    mapped.push_back(pool.intern(bool(f),std::move(edges)));
  }
  U previous=0,mass=0;
  for(size_t i=0;i<nb;++i) {
    U score=r.get(16); size_t root=size_t(r.get(4));
    if(!score||score>p.denominator||(i&&score>=previous)||root>=nn||!pool.nodes[mapped[root]].count) fail("invalid score band");
    p.bands.push_back({score,mapped[root]}); previous=score; mass=add(mass,mul(score,pool.nodes[mapped[root]].count));
  }
  if(mass>p.denominator||r.pos!=r.raw.size()) fail("plan mass/trailing data mismatch");
  if((p.complete&&p.next_score)||p.next_score>p.denominator||(!p.bands.empty()&&p.next_score>=p.bands.back().score)) fail("invalid remainder bound");
  validate_bands(pool,p);
  return p;
}

struct Sink {
  bool emit=false; uint64_t checked=0,bytes=0,hash=14695981039346656037ull;
  std::vector<char> buffer;
  explicit Sink(bool output):emit(output) { if(emit) buffer.reserve(1<<20); }
  void flush() { if(buffer.empty()) return; std::cout.write(buffer.data(),std::streamsize(buffer.size())); if(!std::cout) fail("output pipe failed"); buffer.clear(); }
  void take(const std::string &value) {
    guard.tick();
    if(value.size()>128) fail("oversized candidate");
    // Non-cryptographic observation checksum, NEVER a membership/equality test.
    hash=(hash^uint8_t(value.size()))*1099511628211ull;
    for(unsigned char c:value) hash=(hash^c)*1099511628211ull;
    ++checked; bytes+=value.size();
    if(emit) {
      buffer.push_back(char(value.size())); buffer.insert(buffer.end(),value.begin(),value.end());
      if(buffer.size()>=(1<<20)) flush();
    }
  }
};
template<class Fn> void walk(const Pool &pool,uint32_t id,uint64_t start,uint64_t count,std::string &prefix,Fn &fn) {
  guard.tick();
  const Node &node=pool.nodes[id];
  if(node.final) { if(start) --start; else if(count) { fn(prefix); --count; } }
  for(auto e:node.edges) {
    if(!count) break; uint64_t n=pool.nodes[e.child].count;
    if(start>=n) { start-=n; continue; }
    auto take=std::min(count,n-start); prefix.push_back(char(e.byte)); walk(pool,e.child,start,take,prefix,fn); prefix.pop_back();
    start=0; count-=take;
  }
}
template<class Fn> void replay(const Pool &pool,const Plan &p,uint64_t start,uint64_t count,Fn fn) {
  uint64_t n=total(pool,p); if(start>n||count>n-start) fail("replay outside plan");
  std::string prefix; prefix.reserve(128);
  for(auto b:p.bands) {
    if(!count) break; uint64_t size=pool.nodes[b.root].count;
    if(start>=size) { start-=size; continue; }
    auto take=std::min(count,size-start); auto one=[&](const std::string &s){fn(s,b.score);};
    walk(pool,b.root,start,take,prefix,one); start=0; count-=take;
  }
}
std::string hex(const std::string &s) {
  const char *digits="0123456789abcdef"; std::string result;
  for(unsigned char c:s) { result.push_back(digits[c>>4]); result.push_back(digits[c&15]); } return result;
}
double elapsed(Clock::time_point t) {return std::chrono::duration<double>(Clock::now()-t).count();}
void plan_stats(const Pool &pool,const Plan &p,Clock::time_point t,uint64_t file_bytes=0) {
  std::cout<<"{\"candidates\":\""<<total(pool,p)<<"\",\"bands\":"<<p.bands.size()
           <<",\"complete_remaining_support\":"<<(p.complete?"true":"false")<<",\"next_score_upper_bound\":\""<<dec(p.next_score)<<"\""
           <<",\"pool_nodes\":"<<pool.nodes.size()<<",\"pool_edges\":"<<pool.edge_count
           <<",\"file_bytes\":"<<file_bytes<<",\"seconds\":"<<elapsed(t)<<",\"peak_rss_bytes\":"<<rss();
  validation_stats(std::cout); std::cout<<"}\n";
}
int main(int argc,char **argv) {
  std::ios::sync_with_stdio(false); std::cin.tie(nullptr);
  try {
    if(argc<2) fail("commands: compile, subtract, describe, inspect, probe, emit, consume");
    auto began=Clock::now(); std::string cmd=argv[1];
    if(cmd=="compile"&&argc==4) {
      Source source=read_source(argv[2]); Pool pool; Compiler c{pool,source,{},0,0}; Plan p=c.prepare();
      auto bytes=write_plan(argv[3],pool,p); plan_stats(pool,p,began,bytes);
    } else if(cmd=="prepare"&&argc>=5&&(argc-5)%3==0) {
      auto limit=u64(argv[4]); if(limit<1||limit>4096) fail("score level limit must be 1..4096");
      Source source=read_source(argv[2]); Pool pool; uint32_t history=0;
      for(int i=5;i<argc;i+=3) { Plan old=read_plan(argv[i],pool); history=pool.unite(history,selection(pool,old,u64(argv[i+1]),u64(argv[i+2]))); }
      Compiler c{pool,source,{},0,size_t(limit)}; Plan p=c.prepare(history);
      auto bytes=write_plan(argv[3],pool,p); plan_stats(pool,p,began,bytes);
    } else if(cmd=="subtract"&&argc>=4&&(argc-4)%3==0) {
      Pool pool; Plan p=read_plan(argv[2],pool); uint32_t history=0;
      for(int i=4;i<argc;i+=3) { Plan old=read_plan(argv[i],pool); history=pool.unite(history,selection(pool,old,u64(argv[i+1]),u64(argv[i+2]))); }
      Plan result{p.denominator,{},p.complete,p.next_score};
      for(auto b:p.bands) { auto root=pool.subtract(b.root,history); if(root) result.bands.push_back({b.score,root}); }
      auto bytes=write_plan(argv[3],pool,result); plan_stats(pool,result,began,bytes);
    } else if(cmd=="describe"&&argc==3) {
      Pool pool; Plan p=read_plan(argv[2],pool); plan_stats(pool,p,began,fs::file_size(argv[2]));
    } else if((cmd=="probe"||cmd=="emit"||cmd=="inspect")&&argc==5) {
      Pool pool; Plan p=read_plan(argv[2],pool); auto loaded=elapsed(began);
      uint64_t start=u64(argv[3]),count=u64(argv[4]);
      if(count>1000000000ull || (cmd=="inspect"&&count>10000)) fail("bounded replay limit");
      Sink sink(cmd=="emit"); auto traversal=Clock::now();
      replay(pool,p,start,count,[&](const std::string &s,U score) {
        if(cmd=="inspect") std::cout<<hex(s)<<" "<<dec(score)<<" "<<dec(p.denominator)<<"\n";
        else sink.take(s);
      });
      sink.flush(); if(cmd=="emit") std::cout.flush();
      if(cmd!="inspect") {
        auto &out=cmd=="emit"?std::cerr:std::cout;
        out<<"{\"frames\":"<<sink.checked<<",\"candidate_bytes\":"<<sink.bytes<<",\"fnv64\":\""<<sink.hash
           <<"\",\"load_seconds\":"<<loaded<<",\"traversal_seconds\":"<<elapsed(traversal)
           <<",\"seconds\":"<<elapsed(began)<<",\"peak_rss_bytes\":"<<rss();
        validation_stats(out); out<<"}\n";
      }
    } else if(cmd=="consume"&&argc==3) {
      uint64_t expected=u64(argv[2]); Sink sink(false); std::array<char,65536> block{};
      std::string value; value.reserve(128); int remaining=-1;
      for(;;) {
        auto bytes=read(STDIN_FILENO,block.data(),block.size());
        if(bytes<0) { if(errno==EINTR) continue; fail("input read failed"); }
        if(!bytes) break;
        size_t pos=0;
        while(pos<size_t(bytes)) {
          if(remaining<0) {
            remaining=uint8_t(block[pos++]);
            if(remaining>128||sink.checked>=expected) fail("invalid/excess input frame");
          }
          size_t take=std::min(size_t(remaining),size_t(bytes)-pos);
          value.append(block.data()+pos,take); pos+=take; remaining-=int(take);
          if(!remaining) { sink.take(value); value.clear(); remaining=-1; }
        }
      }
      if(remaining!=-1||sink.checked!=expected) fail("missing/truncated input frames");
      std::cout<<"{\"consumed_frames\":"<<sink.checked<<",\"candidate_bytes\":"<<sink.bytes<<",\"fnv64\":\""<<sink.hash
               <<"\",\"seconds\":"<<elapsed(began)<<",\"scope\":\"delivery only, NOT password checks\"}\n";
    } else fail("invalid command/arguments");
    return 0;
  } catch(const std::exception &e) { std::cerr<<"ERROR: "<<e.what()<<"\n"; return 2; }
}
