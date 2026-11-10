#define RELAY_BOARD_06 1
#define RELAY_CELLULAR 1
#include "board_profile.h"
#include "pcb06_capture_spool.h"
#include "pcb06_button_cues.h"
#include "pcb06_button_sounds.h"
#include "pcb06_ptt_player.h"
#include "pcb06_ptt_sounds.h"
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

#define IRAM_ATTR
constexpr int ESP_OK=0, ESP_ERR_TIMEOUT=2, LOW=0;
constexpr int I2S_NUM_0=0, I2S_ROLE_MASTER=1, I2S_DATA_BIT_WIDTH_16BIT=16, I2S_SLOT_MODE_STEREO=2;
constexpr int SAMPLE_RATE_HZ=24000, MIC_SHIFT=0;
constexpr uint32_t MAX_RECORDING_MS=Pcb06CaptureSpool::kMaxCaptureMs;
constexpr uint32_t MAX_RECORDING_SAMPLES=Pcb06CaptureSpool::kMaxSamples;
constexpr size_t MIC_FRAME=2048;
using gpio_num_t=int;
struct Channel { bool enabled=false; };
using i2s_chan_handle_t=Channel*;
struct i2s_event_data_t {};
struct i2s_event_callbacks_t { bool (*on_recv_q_ovf)(i2s_chan_handle_t,i2s_event_data_t*,void*)=nullptr; };
struct i2s_chan_config_t { int id, dma_desc_num=0, dma_frame_num=0; };
struct i2s_pdm_rx_config_t {
  int clk_cfg;
  struct { int bits, mode; } slot_cfg;
  struct { int clk,din; struct { bool invert; } invert_flags; } gpio_cfg;
};
#define I2S_CHANNEL_DEFAULT_CONFIG(a,b) i2s_chan_config_t{a}
#define I2S_PDM_RX_CLK_DEFAULT_CONFIG(a) (a)
#define I2S_PDM_RX_SLOT_DEFAULT_CONFIG(a,b) {a,b}
#define ESP_ERROR_CHECK(x) assert((x)==ESP_OK)
static int locks=0, live=0, readError=ESP_OK, samplesPerRead=256, closes=0;
static uint32_t now=0, recStartMs=0, board06LastMicData=0;
static bool held=true, recording=false, playing=false, talkSent=false, wsOpen=true, board06AmpOn=false;
static i2s_chan_handle_t rxChan=nullptr;
static bool micCapturing=false;
enum class MuseMode { Off, Paired };
static std::atomic<MuseMode> museMode{MuseMode::Off};
static std::atomic<bool> pttClosedMuse{false};
static std::atomic<bool> board06DmaOverflow{false};
static uint32_t micSamplesRead=0, micClipped=0, micFramesSent=0;
static size_t board06MicTrimRemaining=0;
static uint64_t micEnergy=0;
static int32_t micPeak=0;
static size_t micBatchFill=0;
static int16_t board06TxChunk[MIC_FRAME];
static std::vector<int16_t> storage(Pcb06CaptureSpool::kMaxSamples);
static Pcb06CaptureSpool spool(storage.data(),storage.size());
static Pcb06CaptureSpool* captureSpool=&spool;
static Pcb06CueQueue board06CueQueue;
static Pcb06CuePlayer board06CuePlayer;
static Pcb06PttPlayer board06PttPlayer(
    pcb06_ptt_sounds::kPress.data(),pcb06_ptt_sounds::kPressSamples,
    pcb06_ptt_sounds::kRelease.data(),pcb06_ptt_sounds::kReleaseSamples,
    pcb06_ptt_sounds::kLoop.data(),pcb06_ptt_sounds::kLoopSamples);
static bool board06PttOwnsOutput=false;
static bool muted=false, offRequested=false, rampInput=false, clippingInput=false;
static int volume=5, speakerWrites=0, speakerDrainCalls=0;
static Channel speakerChannel;
static Channel* txChan=&speakerChannel;
static std::vector<int16_t> expectedPcm;
static uint32_t rawSampleIndex=0;
static uint32_t inputFrames=0, expectedTrim=5280;
static std::function<void()> blockedSpeaker;
struct MicLock { MicLock(){assert(locks==0);++locks;} ~MicLock(){--locks;} };
struct { void reset(){} int32_t process(int32_t sample){return sample;} } micHighPass;
struct { int talk=1; } buttons;
struct { bool canRequest(bool connected){return connected;} } endpointAvailability;
struct { template<class...T> void printf(const char*,T...){} void println(const char*){} } Serial;
static std::vector<std::string> events;
static std::vector<int16_t> sentPcm;
static std::function<void()> blockedSend;
static bool failBinary=false, failText=false;
uint32_t millis(){return now;}
void vTaskDelay(int){++now;}
int digitalRead(int){return held ? LOW : 1;}
int playbackMutex=0, portMAX_DELAY=0;
void xSemaphoreTake(int,int){}
void xSemaphoreGive(int){}
void ringClear(){}
void pausePlayback(){playing=false;}
bool board06DrainSpeaker(){++speakerDrainCalls;board06AmpOn=false;return true;}
void board06SetAmp(bool enabled){board06AmpOn=enabled;}
int i2s_channel_write(Channel*,const void*,size_t bytes,size_t* written,int wait){
  assert(wait==10 && bytes<=1920 && locks==0);
  ++speakerWrites;
  if(blockedSpeaker)blockedSpeaker();
  *written=bytes;return ESP_OK;
}
static void board06TransportFailed();
void connectionEnded();
int i2s_new_channel(i2s_chan_config_t* cfg,void*,i2s_chan_handle_t* out){
  assert(locks==1 && cfg->id==0);*out=new Channel;++live;return ESP_OK;
}
int i2s_channel_init_pdm_rx_mode(Channel*,i2s_pdm_rx_config_t* cfg){
  assert(cfg->clk_cfg==24000 && cfg->slot_cfg.bits==16 && cfg->slot_cfg.mode==2);
  assert(cfg->gpio_cfg.clk==6 && cfg->gpio_cfg.din==7);return ESP_OK;
}
int i2s_channel_register_event_callback(Channel*,const i2s_event_callbacks_t* cb,void*){
  assert(cb->on_recv_q_ovf);return ESP_OK;
}
int i2s_channel_enable(Channel* c){assert(locks==1);c->enabled=true;return ESP_OK;}
int i2s_channel_disable(Channel* c){assert(locks==1 && c->enabled);c->enabled=false;return ESP_OK;}
int i2s_del_channel(Channel* c){assert(locks==1 && !c->enabled);delete c;--live;return ESP_OK;}
int i2s_channel_read(Channel* c,void* data,size_t cap,size_t* bytes,int wait){
  assert(locks==1 && c->enabled && wait==4);
  if(readError!=ESP_OK){*bytes=0;return readError;}
  assert(size_t(samplesPerRead)*4<=cap);
  auto* pcm=static_cast<int16_t*>(data);
  for(int i=0;i<samplesPerRead;++i){
    pcm[2*i]=rampInput ? int16_t((rawSampleIndex++%4096)-2048) : 1234;
    pcm[2*i+1]=rampInput ? int16_t(-pcm[2*i]-1) : -2345;
    if(clippingInput){
      constexpr int16_t levels[]={-32768,-10000,-2000,0,2000,10000,32767};
      pcm[2*i]=levels[rawSampleIndex++%7];
      pcm[2*i+1]=pcm[2*i];
    }
    const int amplified=int(pcm[2*i+RELAY_PCB06_MIC_SLOT])*8;
    if(inputFrames++>=expectedTrim)
      expectedPcm.push_back(amplified>32767 ? 32767 : amplified<-32768 ? -32768 : amplified);
  }
  *bytes=samplesPerRead*4;return ESP_OK;
}
bool wsSendText(const char* text){
  assert(locks==0);
  events.emplace_back(text);
  return !failText;
}
bool wsSendBinary(const uint8_t* data,size_t bytes){
  assert(locks==0);
  std::vector<uint8_t> original(data,data+bytes);
  if(blockedSend){auto callback=blockedSend;blockedSend=nullptr;callback();}
  assert(std::memcmp(original.data(),data,bytes)==0); // Worker cannot overwrite TX snapshot.
  if(failBinary)return false;
  auto* pcm=reinterpret_cast<const int16_t*>(data);
  sentPcm.insert(sentPcm.end(),pcm,pcm+bytes/2);
  return true;
}
// SKETCH_FUNCTIONS
void connectionEnded(){++closes;wsOpen=false;stopRecording();}
static int countEvent(const char* name){int n=0;for(auto& event:events)if(event.find(name)!=std::string::npos)++n;return n;}
static void collect(unsigned steps=30){for(unsigned i=0;i<steps;++i){now+=10;board06CaptureStep();}}
static void drain(){for(int i=0;recording && i<1000;++i)pumpMic();assert(!recording);}
static void assertCleared(){for(auto value:storage)assert(value==0);for(auto value:board06TxChunk)assert(value==0);}
int main(int argc,char**argv){
  assert(argc==2);std::string mode=argv[1];
  rampInput=mode=="cue-during-capture" || mode=="cue-already-playing" || mode=="ptt-during-capture"
      || mode=="opening-crop" || mode=="crop-reset";
  clippingInput=mode=="gain-clipping";
  if(mode=="muted-capture"){muted=true;expectedTrim=0;}
  if(mode=="volume-zero-capture"){volume=0;expectedTrim=0;}
  if(mode=="cue-already-playing"){
    Pcb06CueRequest request{Pcb06CueKind::Play,5,now,0};
    board06CuePlayer.start(request,pcb06_sounds::kPlayTalk,pcb06_sounds::kPlayTalkSamples);
    board06AmpOn=true;
  }
  startRecording();assert(recording && micCapturing && live==1);
  if(mode=="ptt-during-capture"){
    assert(now==0 && recStartMs==0 && speakerDrainCalls==0);
    collect(1);board06ObservePtt();now+=20;board06ObservePtt();
    blockedSpeaker=[](){collect(1);};
    for(int i=0;i<190;++i){board06ObservePtt();assert(board06PlayPttChunk());}
    assert(micCapturing && board06PttPlayer.phase()==Pcb06PttPlayer::Phase::Bed);
    held=false;guardMicRelease();assert(!micCapturing);board06ObservePtt();
    for(int i=0;i<50 && board06PttPlayer.active();++i){board06ObservePtt();board06PlayPttChunk();}
    blockedSpeaker=nullptr;assert(!micCapturing && !board06PttPlayer.active());
    drain();assert(sentPcm==expectedPcm && sentPcm.size()>1000);
    assert(countEvent("talk.start")==1 && countEvent("talk.end")==1 && closes==0);assertCleared();
  }else if(mode=="opening-crop"){
    collect(20);pumpMic();assert(spool.captured()==0 && events.empty());
    collect(1);assert(spool.captured()==96); // Crop ends 160 frames into this DMA block.
    now=300;held=false;guardMicRelease();drain();
    assert(sentPcm==expectedPcm && sentPcm.size()==96);
    assert(countEvent("talk.start")==1 && countEvent("talk.end")==1);assertCleared();
  }else if(mode=="crop-only"){
    collect(20);samplesPerRead=160;now=300;board06CaptureStep();
    held=false;guardMicRelease();drain();
    assert(micSamplesRead==5280 && sentPcm.empty() && events.empty());assertCleared();
  }else if(mode=="crop-reset"){
    for(int round=0;round<2;++round){
      if(round){held=true;now+=100;inputFrames=rawSampleIndex=0;expectedPcm.clear();sentPcm.clear();startRecording();}
      collect(40);held=false;guardMicRelease();drain();
      assert(sentPcm==expectedPcm && sentPcm.size()==40*256-5280);assertCleared();
    }
  }else if(mode=="muted-capture" || mode=="volume-zero-capture"){
    collect();held=false;guardMicRelease();drain();
    assert(sentPcm==expectedPcm && sentPcm.size()==30*256);assertCleared();
  }else if(rampInput){
    assert(now==0 && recStartMs==0 && speakerDrainCalls==0);
    if(!board06CuePlayer.active())assert(board06CueQueue.push(Pcb06CueKind::Talk,5,false,now));
    collect(1);
    blockedSpeaker=[](){collect(2);};
    for(int i=0;i<20 && board06PlayCueChunk();++i){}
    blockedSpeaker=nullptr;
    assert(speakerWrites>0 && micCapturing && !board06CuePlayer.active());
    collect(30);held=false;guardMicRelease();drain();
    assert(sentPcm==expectedPcm && sentPcm.size()>1000);
    assert(countEvent("talk.start")==1 && countEvent("talk.end")==1 && closes==0);
    assertCleared();
  }else if(mode=="short-tap"){
    collect(10);now=100;held=false;guardMicRelease();
    now=5000;pumpMic();assert(!recording && events.empty() && closes==0);assertCleared();
  }else if(mode=="gain-clipping"){
    collect();held=false;guardMicRelease();drain();
    assert(sentPcm==expectedPcm && micClipped>0);
    assert(micPeak==32768 && countEvent("talk.end")==1);
    assertCleared();
  }else if(mode=="blocked-network"){
    collect();
    blockedSend=[](){collect(40);held=false;guardMicRelease();assert(!micCapturing);now+=5000;};
    pumpMic();assert(recording); // Spool backlog remains after the first blocked send.
    const size_t captured=micSamplesRead;assert(captured==70*256);
    {MicLock guard;assert(spool.durationMs(now)==700);}
    drain();assert(sentPcm.size()==captured-5280);
    for(auto sample:sentPcm)assert(sample==8*(RELAY_PCB06_MIC_SLOT ? -2345 : 1234));
    assert(countEvent("talk.start")==1 && countEvent("talk.end")==1 && closes==0);
    assertCleared();
  }else if(mode=="failed-write" || mode=="failed-read" || mode=="dma-overflow" || mode=="failed-during-write" || mode=="overflow-release"){
    collect();
    if(mode=="failed-write")failBinary=true;
    if(mode=="failed-during-write")blockedSend=[](){readError=5;board06CaptureStep();};
    pumpMic();
    if(mode=="failed-read" || mode=="dma-overflow" || mode=="overflow-release"){
      if(mode=="failed-read")readError=5;
      else board06MicOverflow(nullptr,nullptr,nullptr);
      if(mode=="overflow-release"){held=false;guardMicRelease();}
      else board06CaptureStep();
      held=false;stopRecording();
      assert(recording); // Failure stays busy until main closes the incomplete span.
      pumpMic();
    }
    assert(!recording && !micCapturing && !wsOpen && closes==1);
    assert(countEvent("talk.end")==0);assertCleared();
  }else if(mode=="deadline"){
    collect();
    now=59999;board06CaptureStep();now=60000;guardMicRelease();
    assert(!micCapturing);now=90000;
    {MicLock guard;assert(spool.durationMs(now)==60000);}
    drain();assert(countEvent("talk.start")==1 && countEvent("talk.end")==1);
  }else if(mode=="sample-cap"){
    for(size_t i=0;i<Pcb06CaptureSpool::kMaxSamples/256;++i){
      now=uint32_t((i*256+256)*1000/24000); if(now==60000)now=59999;
      board06CaptureStep();
    }
    assert(micSamplesRead==Pcb06CaptureSpool::kMaxSamples && !micCapturing);
    drain();assert(sentPcm.size()==Pcb06CaptureSpool::kMaxSamples-5280);
    assert(countEvent("talk.end")==1);assertCleared();
  }else if(mode=="disconnected"){
    collect();pumpMic();wsOpen=false;stopRecording();
    assert(!recording && !micCapturing && countEvent("talk.end")==0);assertCleared();
  }else assert(false);
  assert(live==0 && locks==0);
}
