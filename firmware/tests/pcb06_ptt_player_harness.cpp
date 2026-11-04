#include "pcb06_ptt_player.h"
#include <array>
#include <cassert>
#include <cstring>
#include <string>
#include <vector>

using Phase=Pcb06PttPlayer::Phase;
static std::array<int16_t,5280> opening;
static std::array<int16_t,7920> closing;
static std::array<int16_t,18323> bed;
static uint32_t now=0;
static bool down=false,silent=false;
static std::vector<int16_t> chunk(Pcb06PttPlayer& player){
  player.observe(down,silent,now);
  const auto* bytes=player.data(10);
  if(!bytes)return {};
  const size_t count=player.bytes()/8;
  const auto* pcm=reinterpret_cast<const int16_t*>(bytes);
  std::vector<int16_t> out;
  for(size_t i=0;i<count;++i){
    out.push_back(pcm[4*i]);
    for(int j=1;j<4;++j)assert(pcm[4*i+j]==pcm[4*i]);
  }
  assert(player.commit(player.bytes()));now+=10;return out;
}
static void press(Pcb06PttPlayer& player){down=true;player.observe(down,silent,++now);now+=20;player.observe(down,silent,now);}
static void untilBed(Pcb06PttPlayer& player){for(int i=0;i<50 && player.phase()!=Phase::Bed;++i)chunk(player);assert(player.phase()==Phase::Bed);}
int main(int argc,char**argv){
  assert(argc==2);std::string mode=argv[1];
  opening.fill(1000);closing.fill(3000);bed.fill(2000);
  if(mode=="signed-close"){opening.fill(-1000);closing.fill(-3000);bed.fill(-2000);}
  if(mode=="varied-loop")for(size_t i=0;i<bed.size();++i)bed[i]=int16_t((i*37)%4093-2046);
  Pcb06PttPlayer player(opening.data(),opening.size(),closing.data(),closing.size(),bed.data(),bed.size());
  player.observe(false,false,now);press(player);
  if(mode=="signed-close"){
    chunk(player);down=false;player.observe(false,false,now);
    auto fade=chunk(player);
    for(size_t i=0;i<fade.size();++i)assert(fade[i]==(-3000*int(i)-1000*int(240-i))/240);
  }else if(mode=="varied-loop"){
    std::vector<uint8_t> bytes;
    const size_t wanted=(opening.size()+bed.size()*3)*8;
    while(bytes.size()<wanted){
      const uint8_t* data=player.data(10);
      const size_t n=player.bytes()<137?player.bytes():137;
      bytes.insert(bytes.end(),data,data+n);assert(player.commit(n));
    }
    for(size_t i=0;i<opening.size()+bed.size()*3;++i){
      int16_t got;std::memcpy(&got,bytes.data()+i*8,2);
      int16_t expected=1000;
      if(i>=opening.size()){
        const size_t at=i-opening.size();expected=int32_t(bed[at%bed.size()])/16;
        if(at<240)expected=int32_t(expected)*int32_t(at+1)/240;
      }
      assert(got==expected);
    }
  }else if(mode=="loop"){
    std::vector<int16_t> data;
    for(int i=0;i<200;++i){auto next=chunk(player);data.insert(data.end(),next.begin(),next.end());}
    for(size_t i=0;i<opening.size();++i)assert(data[i]==1000);
    for(size_t i=opening.size()+240;i<data.size();++i)assert(data[i]==125);
    // More than two loop boundaries retain the quieter bed after the single fade.
    assert(data.size()>opening.size()+bed.size()*2);
    down=false;player.observe(down,false,now);
    auto fade=chunk(player);assert(fade.front()==125 && fade.back()>2900);
    std::vector<int16_t> tail;
    for(int i=0;i<50 && player.active();++i){auto next=chunk(player);tail.insert(tail.end(),next.begin(),next.end());}
    assert(!player.active() && !player.ampAllowed());
    for(size_t i=0;i<closing.size()-240;++i)assert(tail[i]==3000);
    for(size_t i=closing.size()-240;i<tail.size();++i)assert(tail[i]==0);
  }else if(mode=="mute"){
    untilBed(player);silent=true;player.observe(down,silent,now);
    auto zero=chunk(player);for(auto sample:zero)assert(sample==0);assert(!player.ampAllowed());
    silent=false;player.observe(down,silent,now);
    for(int i=0;i<6 && player.phase()!=Phase::Bed;++i)chunk(player);
    assert(player.phase()==Phase::Bed);for(auto sample:chunk(player))assert(sample>=0 && sample<=125);
  }else if(mode=="quick-tap"){
    down=false;player.observe(down,false,now);
    auto first=chunk(player);for(auto sample:first)assert(sample==1000);
    auto cross=chunk(player);assert(cross.front()==1000 && cross.back()>2900);
  }else if(mode=="repress-open" || mode=="repress-bed" || mode=="repress-close" || mode=="repress-drain" || mode=="release-during-flush"){
    if(mode=="repress-open")chunk(player);else untilBed(player);
    down=false;player.observe(down,false,now);now+=20;player.observe(down,false,now);
    if(mode=="repress-close")chunk(player);
    if(mode=="repress-drain")while(player.phase()!=Phase::Drain)chunk(player);
    const auto oldGeneration=player.generation();press(player);
    assert(player.phase()==Phase::Flush && player.generation()!=oldGeneration);
    if(mode=="release-during-flush"){down=false;player.observe(down,false,now);}
    for(int i=0;i<5;++i){auto zero=chunk(player);for(auto sample:zero)assert(sample==0);assert(!player.ampAllowed());}
    auto first=chunk(player);for(auto sample:first)assert(sample==1000);
    if(mode=="release-during-flush"){auto cross=chunk(player);assert(cross.back()>2900);}
  }else if(mode=="partial"){
    const auto* data=player.data(10);std::vector<uint8_t> original(data,data+player.bytes());
    assert(player.commit(17));data=player.data(10);
    assert(player.bytes()==original.size()-17 && std::memcmp(data,original.data()+17,player.bytes())==0);
    down=false;player.observe(false,false,now); // Old prepared suffix is discarded by the new phase.
    data=player.data(10);assert(data && player.bytes()==1920);
  }else assert(false);
}
