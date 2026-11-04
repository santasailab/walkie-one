#define RELAY_BOARD_06
#define RELAY_CELLULAR
#include "pcb06_button_cues.h"
#include "pcb06_button_sounds.h"
#include "pcb06_ptt_player.h"
#include "pcb06_ptt_sounds.h"
#include "volume_button.h"
#include "ptt_gesture.h"
#include "listen_gesture.h"
#include "playback_state.h"
#include "played_receipt.h"
#include "endpoint_availability.h"
#include <cassert>
#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

constexpr int LOW=0, HIGH=1, PIN_BTN_POWER=-1, ESP_OK=0, ESP_ERR_TIMEOUT=2;
static uint32_t now=100, volumeChangedAt=0;
static int volume=5, levels[6]={1,1,1,1,1,1}, playbackMutex=1, txChan=1, portMAX_DELAY=0;
static bool muted=false, recording=false, playing=false, offRequested=false, volumeFeedback=false;
static bool wsOpen=true, board06AmpOn=false;
static struct { int talk=1, play=5, volumeUp=2, volumeDown=4; } buttons;
static Pcb06CueQueue board06CueQueue;
static Pcb06CuePlayer board06CuePlayer;
static Pcb06PttPlayer board06PttPlayer(
    pcb06_ptt_sounds::kPress.data(),pcb06_ptt_sounds::kPressSamples,
    pcb06_ptt_sounds::kRelease.data(),pcb06_ptt_sounds::kReleaseSamples,
    pcb06_ptt_sounds::kLoop.data(),pcb06_ptt_sounds::kLoopSamples);
static bool board06PttOwnsOutput=false;
static Pcb06ButtonSample board06ButtonSample;
static bool board06ListenPending=false, board06ListenWaitForCue=false, board06PlayCueAccepted=false;
static uint32_t board06ListenRequestedAt=0;
static PlaybackState playback;
static PlayedReceipt playedReceipt;
static EndpointAvailability endpointAvailability;
static char pendingAck[65]="test-reply";
static size_t writeLimit=1920;
static uint32_t writeDelay=1;
static int writes=0, locks=0, failWrite=0;
static std::vector<uint8_t> output;
static std::vector<bool> ampAtWrite;
static int millisTick=0, flipReadPin=-1, listenRequests=0;
static std::function<void()> onListen;
uint32_t millis(){const auto value=now;now+=millisTick;return value;}
int digitalRead(int pin){
  assert(pin>=0 && pin<6);const int value=levels[pin];
  if(pin==flipReadPin){levels[pin]=LOW;flipReadPin=-1;}
  return value;
}
int constrain(int v,int low,int high){return v<low?low:v>high?high:v;}
void xSemaphoreTake(int,int){assert(locks++==0);}
void xSemaphoreGive(int){assert(--locks==0);}
void board06SetAmp(bool on){board06AmpOn=on;}
void vTaskDelay(int){++now;}
static struct { template<class...T>void printf(const char*,T...){} void println(const char*){} } Serial;
int i2s_channel_write(int,const void* data,size_t size,size_t* written,int timeout){
  assert(locks==1 && timeout==10 && size<=1920);
  ++writes;now+=writeDelay;*written=failWrite?0:(size<writeLimit?size:writeLimit);
  const auto* bytes=static_cast<const uint8_t*>(data);
  output.insert(output.end(),bytes,bytes+*written);ampAtWrite.push_back(board06AmpOn);
  return failWrite?5:ESP_OK;
}
size_t ringAvail(){return 0;}
void ringClear(){}
void pausePlayback(){playing=false;playback.pause();}
bool playbackPlayStaged(){if(recording||playedReceipt.pending())return false;playing=playback.play();return playing;}
void wsSendText(const char*){++listenRequests;if(onListen)onListen();}
void startRecording(){assert(levels[buttons.talk]==LOW);recording=true;}
void stopRecording(){recording=false;}
enum class MuseMode {Off,Paired};
static MuseMode museMode=MuseMode::Off;
void museStop(){museMode=MuseMode::Off;}
void museStart(){}

// SKETCH_FUNCTIONS

static void service(uint32_t elapsed=25){
  now+=elapsed;buttonService();board06ButtonCueService();board06ServiceListenRequest();
}
static void down(int pin){levels[pin]=LOW;service();service();}
static void up(int pin){levels[pin]=HIGH;service();service();}
static Pcb06CueRequest take(){Pcb06CueRequest cue;assert(board06CueQueue.pop(cue,now));return cue;}
static void empty(){Pcb06CueRequest cue;assert(!board06CueQueue.pop(cue,now));}
static void playAll(){for(int i=0;i<2000;++i){if(!board06PlayCueChunk())return;}assert(false);}
static std::vector<uint8_t> expected(const int16_t* pcm,size_t count,int loudness){
  std::vector<int16_t> samples;
  for(size_t i=0;i<count;++i)for(int slot=0;slot<4;++slot)samples.push_back(int32_t(pcm[i])*loudness/10);
  samples.resize(samples.size()+Pcb06CuePlayer::kDrainSamples*4,0);
  auto* begin=reinterpret_cast<const uint8_t*>(samples.data());
  return {begin,begin+samples.size()*sizeof(int16_t)};
}
int main(int argc,char** argv){
  assert(argc==2);std::string mode=argv[1];service();
  if(mode=="buttons"){
    levels[1]=LOW;service();assert(recording);empty(); // Capture starts before the click debounce.
    service();empty();Pcb06CueRequest cue; // Talk is now owned by the independent envelope.
    service(600);empty();up(1);empty();assert(!recording);
    down(5);assert(take().kind==Pcb06CueKind::Play);up(5);empty();
    down(2);cue=take();assert(volume==6 && cue.volume==6 && cue.kind==Pcb06CueKind::VolumeUp);
    service(500);assert(volume==7);empty();up(2);empty();
    volume=10;down(2);cue=take();assert(volume==10 && cue.volume==10);up(2);
    volume=1;down(4);assert(volume==0);empty();up(4);
    volume=5;muted=true;down(1);empty();up(1);muted=false;
    levels[2]=levels[4]=LOW;service();service();assert(volume==5);empty();
  }else if(mode=="ptt-reply-gate"){
    playback.stage(5000);playback.play();playback.played=1000;
    playing=true;board06AmpOn=true;const auto generation=playback.generation;
    levels[1]=LOW;board06ObservePtt();now+=20;board06ObservePtt();
    assert(!board06PlayPttChunk() && writes==0 && board06AmpOn);
    assert(playback.played==1000 && playback.generation==generation);
    pausePlayback();assert(board06PlayPttChunk() && writes==1);
    assert(playback.played==1000 && !playback.finished && std::strcmp(pendingAck,"test-reply")==0);
  }else if(mode=="ptt-production" || mode=="ptt-mute-new-cue"){
    writeDelay=10;levels[1]=LOW;recording=true;
    board06ObservePtt();now+=20;board06ObservePtt();
    for(int i=0;i<190;++i){board06ObservePtt();assert(board06PlayPttChunk());}
    assert(board06PttPlayer.phase()==Pcb06PttPlayer::Phase::Bed && board06PttOwnsOutput);
    if(mode=="ptt-mute-new-cue"){
      volume=0;board06ObservePtt();assert(board06PlayPttChunk());
      const auto flush=board06PttPlayer.flushEpoch();
      volume=1;assert(board06CueQueue.push(Pcb06CueKind::VolumeUp,1,false,now));
      board06ObservePtt();assert(board06PttPlayer.flushEpoch()==flush);
      for(int i=0;i<5 && board06PttPlayer.flushing();++i){board06ObservePtt();board06PlayPttChunk();}
      assert(board06PlayCueChunk()); // Fresh post-unmute cue survived the old flush.
      return 0;
    }
    assert(board06CueQueue.push(Pcb06CueKind::VolumeUp,7,false,now));
    const auto bedPosition=board06PttPlayer.bedPosition();
    const int before=writes;while(board06PlayCueChunk())board06ObservePtt();
    assert(writes-before==3); // 23ms tick, no extra 50ms gap in the held bed.
    assert(board06PttPlayer.phase()==Pcb06PttPlayer::Phase::Bed);
    assert(board06PttPlayer.bedPosition()==bedPosition);
    const auto beforeResume=output.size();assert(board06PlayPttChunk());
    int16_t resumed;std::memcpy(&resumed,output.data()+beforeResume,2);
    assert(resumed==0 && !board06AmpOn);
    levels[1]=HIGH;board06ObservePtt();assert(board06PttPlayer.phase()==Pcb06PttPlayer::Phase::Close);
    for(int i=0;i<50 && board06PttPlayer.active();++i){board06ObservePtt();board06PlayPttChunk();}
    assert(!board06PttPlayer.active() && !board06AmpOn && recording);
  }else if(mode=="ptt-stalled"){
    writeDelay=10;writeLimit=1;levels[1]=LOW;
    board06ObservePtt();now+=20;board06ObservePtt();
    for(int i=0;i<200 && board06PttPlayer.active();++i)board06PlayPttChunk();
    assert(!board06PttPlayer.active() && !board06AmpOn && writes<150);
  }else if(mode=="sample-boundary"){
    flipReadPin=2;service(0);assert(volume==5);empty();
    // Both action and cue observed HIGH, although the pin changed after sampling.
    millisTick=1;service(0);const auto pressedAt=board06ButtonSample.now;
    now=pressedAt+19;service(0);assert(volume==5);empty();
    // Calls to millis crossed the debounce boundary, but the shared sample did not.
    now=pressedAt+20;service(0);assert(volume==6);assert(take().volume==6);
  }else if(mode=="listen-onset"){
    endpointAvailability.applyPresence(true,true);
    levels[5]=LOW;service();assert(board06ListenPending && listenRequests==0);
    onListen=[](){
      assert(board06PlayCueChunk() && writes==1); // Worker can output before TLS unblocks.
      now+=1000;
    };
    service();assert(listenRequests==1 && !board06ListenPending);
  }else if(mode=="render" || mode=="partial"){
    if(mode=="partial")writeLimit=127;
    playback.stage(5000);playback.play();playback.played=1000;
    const auto generation=playback.generation;
    assert(playedReceipt.remember("c_0123456789abcdef0123456789abcdef",now));
    assert(board06CueQueue.push(Pcb06CueKind::Talk,5,false,now));
    recording=true;playAll(); // Pure local cue is allowed while recording.
    assert(output==expected(pcb06_sounds::kPlayTalk,pcb06_sounds::kPlayTalkSamples,5));
    assert(playback.played==1000 && playback.generation==generation && playback.ready && !playback.finished);
    assert(playedReceipt.pending() && std::strcmp(pendingAck,"test-reply")==0 && recording);
    assert(!board06AmpOn);
  }else if(mode=="mute-active" || mode=="zero-active"){
    assert(board06CueQueue.push(Pcb06CueKind::Play,10,false,now));
    assert(board06PlayCueChunk());const auto before=output.size();
    if(mode=="mute-active")muted=true;else volume=0;
    assert(board06PlayCueChunk() && !board06AmpOn);
    playAll();for(size_t i=before;i<output.size();++i)assert(output[i]==0);
    for(size_t i=1;i<ampAtWrite.size();++i)assert(!ampAtWrite[i]);
  }else if(mode=="mute-queued" || mode=="cancel-then-new"){
    assert(board06CueQueue.push(Pcb06CueKind::Talk,10,false,now));
    if(mode=="cancel-then-new")assert(board06PlayCueChunk());
    muted=true;board06ButtonCueService();
    if(mode=="cancel-then-new")assert(board06PlayCueChunk());
    muted=false;board06ButtonCueService();
    assert(board06CueQueue.push(Pcb06CueKind::VolumeDown,7,false,now));
    playAll();const auto tick=expected(pcb06_sounds::kVolumeTick,pcb06_sounds::kVolumeTickSamples,7);
    assert(output.size()>=tick.size());
    assert(std::memcmp(output.data()+output.size()-tick.size(),tick.data(),tick.size())==0);
    if(mode=="mute-queued")assert(output==tick);
  }else if(mode=="bounded" || mode=="write-error"){
    assert(board06CueQueue.push(Pcb06CueKind::Talk,5,false,now));
    if(mode=="bounded"){writeLimit=1;writeDelay=10;}else failWrite=1;
    playAll();assert(!board06AmpOn && writes<=25);
  }else if(mode=="queue"){
    Pcb06CueQueue queue;Pcb06CueRequest cue;
    assert(queue.push(Pcb06CueKind::Talk,5,false,UINT32_MAX-5));
    assert(queue.pop(cue,10) && cue.volume==5); // millis rollover.
    assert(queue.push(Pcb06CueKind::Play,5,false,0));assert(!queue.pop(cue,251));
    for(size_t i=0;i<queue.kCapacity;++i)assert(queue.push(Pcb06CueKind::Play,int(i%10+1),false,now));
    assert(!queue.push(Pcb06CueKind::Talk,5,false,now));
    for(size_t i=0;i<queue.kCapacity;++i){assert(queue.pop(cue,now));assert(cue.volume==i%10+1);}
    assert(!queue.pop(cue,now));assert(!queue.push(Pcb06CueKind::Talk,0,false,now));
  }else assert(false);
}
