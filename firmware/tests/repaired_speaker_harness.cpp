#include "repaired_speaker.h"
#include <cassert>
#include <cmath>
#include <vector>

// Independent whole-span transcription of the actual audition2 filter/writer.
// Production streaming output must match its signed interpolation, rounding,
// initial conditions and final-sample extension, including every block boundary.
struct ReferenceBiquad {
  float b0,b1,b2,a1,a2,x1=0,x2=0,y1=0,y2=0;
  float process(float x) {
    const float y=b0*x+b1*x1+b2*x2-a1*y1-a2*y2;
    x2=x1;x1=x;y2=y1;y1=y;return y;
  }
};

int main() {
  for (size_t count : {1U,2U,3U,239U,240U,241U,479U,480U,481U,24001U}) {
    std::vector<int16_t> input(count), filtered(count), output(count*4);
    for (size_t i=0;i<count;++i)
      input[i]=static_cast<int16_t>(16000*std::sin(i*.37)+12000*std::sin(i*.027));
    ReferenceBiquad hp{.967222573627f,-1.93444514725f,.967222573627f,-1.93337081293f,.935519481573f};
    ReferenceBiquad shelf{.771244454975f,-.720049199466f,.254550401581f,-1.08016230955f,.385907966639f};
    for (size_t i=0;i<count;++i) {
      float y=shelf.process(hp.process(input[i]));
      if(y>32767)y=32767;
      if(y< -32768)y=-32768;
      filtered[i]=static_cast<int16_t>(std::lround(y));
    }
    RepairedSpeaker speaker;
    size_t total=0;
    for(size_t offset=0;offset<count;offset+=240) {
      size_t n=count-offset<240?count-offset:240;
      total+=speaker.render(input.data()+offset,n,offset+n<count,16,1,100,false,
                            output.data()+total*2);
    }
    assert(total==count*32000/24000);
    for(size_t j=0;j<total;++j) {
      size_t phase=j*24000,index=phase/32000;
      int32_t expected=filtered[index];
      if(index+1<count)
        expected+=int64_t(int32_t(filtered[index+1])-expected)*int64_t(phase%32000)/32000;
      expected/=5;
      assert(output[j*2]==expected);
      assert(output[j*2+1]==expected);
    }
    speaker.reset();
    const size_t n=count<240?count:240;
    size_t frames=speaker.render(input.data(),n,n<count,16,1,100,false,output.data());
    speaker.reset();
    std::vector<int16_t> repeat(frames*2);
    assert(speaker.render(input.data(),n,n<count,16,1,100,false,repeat.data())==frames);
    for(size_t i=0;i<repeat.size();++i)assert(repeat[i]==output[i]);
    speaker.reset();
    speaker.render(input.data(),n,n<count,16,1,100,true,output.data());
    for(size_t i=0;i<frames*2;++i)assert(output[i]==0);
  }
}
