// Preparation workspace collection v2 around the frozen checked reader/core.
// Both older binaries remain available to their pinned campaigns. Only this
// entrypoint's offline set-operation lifetime changes; plan coordinates and
// replay/checker bytes do not. No production candidate set is materialized.
#define main checked_core_main
#include "core_checked.cpp"
#undef main

uint64_t collections=0, largest_uncollected_pool=0;

void collect_pool(Pool &pool,std::initializer_list<Plan *> live_plans,std::initializer_list<uint32_t *> live_roots) {
  guard.tick();
  largest_uncollected_pool=std::max(largest_uncollected_pool,uint64_t(pool.nodes.size()));
  Pool live;
  std::vector<uint32_t> mapped(pool.nodes.size(),UINT32_MAX);
  mapped[0]=0; mapped[1]=1;
  std::function<uint32_t(uint32_t)> copy=[&](uint32_t id) -> uint32_t {
    guard.tick();
    if(mapped[id]!=UINT32_MAX) return mapped[id];
    const auto &node=pool.nodes[id];
    std::vector<Edge> edges; edges.reserve(node.edges.size());
    for(auto edge:node.edges) edges.push_back({edge.byte,copy(edge.child)});
    auto result=live.intern(node.final,std::move(edges));
    if(live.nodes[result].count!=node.count) fail("workspace collection changed a language count");
    return mapped[id]=result;
  };
  // Every live language is copied with all of its descendants. Only unreachable
  // temporary nodes and memo entries are discarded. Hash-consing uses full-key
  // equality. Node IDs are internal, never external rank/receipt identities.
  for(auto *root:live_roots) *root=copy(*root);
  for(auto *plan:live_plans) for(auto &band:plan->bands) band.root=copy(band.root);
  pool=std::move(live);
  ++collections;
}


uint32_t collecting_selection(Pool &pool,Plan &plan,uint64_t start,uint64_t count,uint32_t &history) {
  auto size=total(pool,plan);
  if(start>size || count>size-start) fail("assignment outside plan");
  uint32_t selected=0;
  for(size_t i=0;i<plan.bands.size() && count;++i) {
    auto band=plan.bands[i]; auto n=pool.nodes[band.root].count;
    if(start>=n) { start-=n; continue; }
    auto take=std::min(count,n-start);
    selected=pool.unite(selected,pool.slice(band.root,start,take));
    count-=take; start=0;
    // One wide historical interval can span thousands of score bands. Collect
    // its temporary prefix unions too, while retaining all three live inputs.
    if(pool.nodes.size()>NODE_CAP/2 || pool.unions.size()>STATE_CAP)
      collect_pool(pool,{&plan},{&history,&selected});
  }
  return selected;
}

void preparation_stats(const Pool &pool,const Plan &plan,Clock::time_point began,uint64_t bytes) {
  // Emit the same metrics plus explicit offline-workspace measurements.
  std::cout<<"{\"candidates\":\""<<total(pool,plan)<<"\",\"bands\":"<<plan.bands.size()
           <<",\"complete_remaining_support\":"<<(plan.complete?"true":"false")
           <<",\"next_score_upper_bound\":\""<<dec(plan.next_score)<<"\""
           <<",\"pool_nodes\":"<<pool.nodes.size()<<",\"pool_edges\":"<<pool.edge_count
           <<",\"file_bytes\":"<<bytes<<",\"seconds\":"<<elapsed(began)<<",\"peak_rss_bytes\":"<<rss()
           <<",\"workspace_collections\":"<<collections
           <<",\"largest_uncollected_pool\":"<<largest_uncollected_pool;
  validation_stats(std::cout); std::cout<<"}\n";
}

int main(int argc,char **argv) {
  if(argc<2 || (std::string(argv[1])!="subtract" && std::string(argv[1])!="prepare"))
    return checked_core_main(argc,argv);
  std::ios::sync_with_stdio(false); std::cin.tie(nullptr);
  try {
    auto began=Clock::now();
    std::string command=argv[1];
    if(command=="subtract" && argc>=4 && (argc-4)%3==0) {
      Pool pool; uint32_t history=0;
      for(int i=4;i<argc;i+=3) {
        Plan old=read_plan(argv[i],pool);
        auto selected=collecting_selection(pool,old,u64(argv[i+1]),u64(argv[i+2]),history);
        history=pool.unite(history,selected);
        collect_pool(pool,{},{&history});
      }
      // Load the new ranking only AFTER compacting history. It is not a live
      // input to historical selection and need not occupy its workspace.
      Plan p=read_plan(argv[2],pool);
      Plan remaining{p.denominator,{},p.complete,p.next_score};
      for(size_t i=0;i<p.bands.size();++i) {
        auto band=p.bands[i];
        auto root=pool.subtract(band.root,history);
        if(root) remaining.bands.push_back({band.score,root});
        // A single bounded operation still may exceed the cap. Collection is
        // not a license for unbounded models, or for dropping live languages.
        if(pool.nodes.size()>NODE_CAP/2 || pool.differences.size()>STATE_CAP)
          collect_pool(pool,{&p,&remaining},{&history});
      }
      collect_pool(pool,{&remaining},{&history});
      auto bytes=write_plan(argv[3],pool,remaining);
      preparation_stats(pool,remaining,began,bytes);
    } else if(command=="prepare" && argc>=5 && (argc-5)%3==0) {
      auto limit=u64(argv[4]); if(limit<1||limit>4096) fail("score level limit must be 1..4096");
      Source source=read_source(argv[2]); Pool pool; uint32_t history=0;
      for(int i=5;i<argc;i+=3) {
        Plan old=read_plan(argv[i],pool);
        auto selected=collecting_selection(pool,old,u64(argv[i+1]),u64(argv[i+2]),history);
        history=pool.unite(history,selected);
        collect_pool(pool,{},{&history});
      }
      Compiler compiler{pool,source,{},0,size_t(limit)};
      Plan remaining=compiler.prepare(history);
      auto bytes=write_plan(argv[3],pool,remaining);
      preparation_stats(pool,remaining,began,bytes);
    } else fail("invalid command/arguments");
    return 0;
  } catch(const std::exception &error) {
    std::cerr<<"ERROR: "<<error.what()<<"\n"; return 2;
  }
}
