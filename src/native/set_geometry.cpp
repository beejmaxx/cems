// Generic finite-language set operations. No model-specific rules.
#define main included_checked_main
#include "core_checked.cpp"
#undef main
namespace sets {
Plan language(const Pool &pool,uint32_t root) {
  return {std::max(uint64_t(1),pool.nodes[root].count),root?std::vector<Band>{{1,root}}:std::vector<Band>{}};
}
uint32_t all(Pool &pool,const fs::path &path) {
  auto plan=read_plan(path,pool); return selection(pool,plan,0,total(pool,plan));
}
bool contains(const Pool &pool,uint32_t root,const std::string &value) {
  for(unsigned char c:value) {
    const auto &edges=pool.nodes[root].edges;
    auto it=std::lower_bound(edges.begin(),edges.end(),c,[](Edge e,uint8_t b){return e.byte<b;});
    if(it==edges.end()||it->byte!=c) return false; root=it->child;
  }
  return pool.nodes[root].final;
}
std::string unhex(const std::string &s) {
  if(s.size()>256||s.size()%2) fail("hex length"); std::string result;
  auto nibble=[](char c) -> unsigned {if(c>='0'&&c<='9')return c-'0';if(c>='a'&&c<='f')return c-'a'+10;fail("hex digit");};
  for(size_t i=0;i<s.size();i+=2) result.push_back(char(nibble(s[i])*16+nibble(s[i+1]))); return result;
}
}
int main(int argc,char **argv) {
  std::ios::sync_with_stdio(false); std::cin.tie(nullptr);
  try {
    auto began=Clock::now(); if(argc<2) fail("select, union, overlap, query");
    std::string command=argv[1];
    if(command=="select"&&argc==6) {
      Pool pool; auto p=read_plan(argv[2],pool); auto root=selection(pool,p,u64(argv[3]),u64(argv[4]));
      auto out=sets::language(pool,root); auto bytes=write_plan(argv[5],pool,out); plan_stats(pool,out,began,bytes);
    } else if(command=="union"&&argc>=4&&argc<=68) {
      Pool pool; uint32_t root=0; for(int i=3;i<argc;++i) root=pool.unite(root,sets::all(pool,argv[i]));
      auto out=sets::language(pool,root); auto bytes=write_plan(argv[2],pool,out); plan_stats(pool,out,began,bytes);
    } else if(command=="overlap"&&argc>=4&&argc<=68) {
      Pool pool; auto root=sets::all(pool,argv[2]); auto count=pool.nodes[root].count;
      std::cout<<"{\"base_count\":"<<count<<",\"intersections\":[";
      for(int i=3;i<argc;++i) {
        auto other=sets::all(pool,argv[i]); auto difference=pool.subtract(root,other);
        if(i!=3)std::cout<<","; std::cout<<count-pool.nodes[difference].count;
      }
      std::cout<<"]}\n";
    } else if(command=="query"&&argc==3) {
      Pool pool; auto root=sets::all(pool,argv[2]); std::string line; uint64_t count=0;
      while(std::getline(std::cin,line)) {
        if(++count>100000) fail("query bound"); std::cout<<(sets::contains(pool,root,sets::unhex(line))?1:0)<<"\n";
      }
    } else fail("invalid geometry command");
    return 0;
  } catch(const std::exception &e) {std::cerr<<"ERROR: "<<e.what()<<"\n"; return 2;}
}
