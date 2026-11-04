#include <assert.h>
#include <string.h>

#include <initializer_list>

#include "ota_policy.h"

static const char *kSha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static const char *kOtherSha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

int main() {
  // Version order rejects equal, older and malformed versions.
  assert(otaVersionKey("1.2.3") < otaVersionKey("1.2.4"));
  assert(otaVersionKey("1.10.0") > otaVersionKey("1.9.9"));
  assert(otaVersionKey("9999.9999.9999") > otaVersionKey("1.0.0"));
  for (const char *bad : {"", "1.2", "1.2.3.4", "1.2.3-pcb06", "a.b.c", "10000.0.0", "1..2", (const char *)nullptr})
    assert(otaVersionKey(bad) == -1);

  // The signed message matches tools/restore.py byte for byte.
  char message[128];
  otaSignedMessage(message, sizeof message, "06", "1.2.3", kSha);
  assert(!strcmp(message, "walkieone-firmware\n06\n1.2.3\naaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"));

  // A newer offer is accepted once, after the start jitter.
  OtaPolicy ota{OtaRecord{}};
  assert(ota.boot("1.0.0", 0) == OtaBoot::Normal && !ota.dirty());
  assert(ota.offer(5, "1.1.0", kSha, true, "1.0.0", 1000, 30000) == OtaOffer::Accepted);
  assert(ota.dirty());
  ota.saved();
  assert(!ota.wantsDownload(30999) && ota.wantsDownload(31000));
  // Repeats and a newer revision naming the same image keep the schedule.
  assert(ota.offer(5, "1.1.0", kSha, true, "1.0.0", 2000, 0) == OtaOffer::Unchanged);
  assert(ota.offer(6, "1.1.0", kSha, true, "1.0.0", 2000, 0) == OtaOffer::Unchanged);
  assert(ota.dirty() && ota.record().revision == 6);  // revision saved, schedule kept
  ota.saved();
  // Older revisions cannot undo anything; offers without a revision install nothing.
  assert(ota.offer(4, "1.0.0", "", false, "1.0.0", 3000, 0) == OtaOffer::Ignored);
  assert(ota.offer(-1, "1.2.0", kSha, true, "1.0.0", 3000, 0) == OtaOffer::Ignored);
  assert(!strcmp(ota.record().target, "1.1.0"));

  // Failed downloads back off exponentially (half to full step), capped at 6 h.
  uint32_t wait = ota.downloadFailed(40000, 0);
  assert(wait == kOtaFirstRetryMs / 2 && ota.record().attempts == 1);
  assert(!ota.wantsDownload(40000 + wait - 1) && ota.wantsDownload(40000 + wait));
  wait = ota.downloadFailed(0, 0xffffffffu);
  assert(wait >= kOtaFirstRetryMs && wait <= 2 * kOtaFirstRetryMs);
  for (int i = 0; i < 20; ++i) wait = ota.downloadFailed(0, 0xffffffffu);
  assert(wait <= kOtaMaxRetryMs && wait >= kOtaMaxRetryMs / 2);
  // The wait survives a reboot in full (no wall clock), across millis rollover.
  OtaPolicy rebooted{ota.record()};
  assert(rebooted.boot("1.0.0", 0xfffff000u) == OtaBoot::Normal);
  assert(!rebooted.wantsDownload(0xfffff000u + ota.record().retryWaitMs - 1));
  assert(rebooted.wantsDownload(0xfffff000u + ota.record().retryWaitMs));
  // A repeated offer after reconnect does not reset that wait.
  assert(rebooted.offer(6, "1.1.0", kSha, true, "1.0.0", 0xfffff000u, 0) == OtaOffer::Unchanged);
  assert(!rebooted.wantsDownload(0xfffff000u + 1000));

  // Talking or a low battery defers without counting as a failure.
  OtaPolicy talk{OtaRecord{}};
  talk.offer(1, "1.1.0", kSha, true, "1.0.0", 0, 0);
  talk.downloadDeferred(100);
  assert(talk.record().attempts == 0 && !talk.wantsDownload(100 + kOtaDeferMs - 1));
  assert(talk.wantsDownload(100 + kOtaDeferMs));

  // Staged: installs only after 15 minutes without use.
  talk.downloadStaged();
  assert(!talk.wantsDownload(1u << 30));
  assert(!talk.wantsInstall(kOtaIdleBeforeInstallMs - 1, 0));
  assert(talk.wantsInstall(kOtaIdleBeforeInstallMs, 0));
  assert(talk.wantsInstall(5, 5 - kOtaIdleBeforeInstallMs));  // idle across rollover

  // A pause (no image) withdraws the target and discards the staged image.
  OtaPolicy paused{talk.record()};
  paused.boot("1.0.0", 0);
  assert(paused.offer(2, "1.0.0", "", false, "1.0.0", 0, 0) == OtaOffer::Cleared);
  assert(!paused.wantsInstall(1u << 31, 0) && !paused.record().staged[0]);
  // A different image under the same version is a new target.
  assert(paused.offer(3, "1.1.0", kSha, true, "1.0.0", 0, 0) == OtaOffer::Accepted);
  assert(paused.offer(4, "1.1.0", kOtherSha, true, "1.0.0", 0, 0) == OtaOffer::Accepted);

  // Install, then the new image proves itself.
  OtaPolicy good{talk.record()};
  good.boot("1.0.0", 0);
  good.installing();
  OtaPolicy newImage{good.record()};
  assert(newImage.boot("1.1.0", 0) == OtaBoot::Unproven);
  // The relay now offers the running version: nothing to do, still unproven.
  assert(newImage.offer(7, "1.1.0", "", false, "1.1.0", 0, 0) == OtaOffer::Cleared);
  newImage.proven();
  assert(!newImage.record().installing[0] && !newImage.record().target[0]);
  OtaPolicy after{newImage.record()};
  assert(after.boot("1.1.0", 0) == OtaBoot::Normal);

  // A bad image rolls back; it is blacklisted only after two failed boots.
  OtaPolicy bad{OtaRecord{}};
  bad.offer(1, "2.0.0", kSha, true, "1.0.0", 0, 0);
  bad.downloadStaged();
  bad.installing();
  OtaPolicy back{bad.record()};
  assert(back.boot("1.0.0", 0) == OtaBoot::RolledBack);
  assert(back.record().failedBoots == 1 && !back.record().blacklisted[0]);
  assert(!back.record().staged[0] && back.wantsDownload(0));  // one more try
  assert(back.offer(1, "2.0.0", kSha, true, "1.0.0", 0, 0) == OtaOffer::Unchanged);
  back.downloadStaged();
  back.installing();
  OtaPolicy twice{back.record()};
  assert(twice.boot("1.0.0", 0) == OtaBoot::RolledBack);
  assert(!strcmp(twice.record().blacklisted, "2.0.0"));
  assert(twice.offer(2, "2.0.0", kSha, true, "1.0.0", 0, 0) == OtaOffer::Cleared);
  assert(!twice.wantsDownload(1u << 30));
  // A newer release repairs it.
  assert(twice.offer(3, "2.0.1", kSha, true, "1.0.0", 0, 0) == OtaOffer::Accepted);

  // Equal or older versions are never targets.
  OtaPolicy same{OtaRecord{}};
  assert(same.offer(1, "1.0.0", kSha, true, "1.0.0", 0, 0) == OtaOffer::Unchanged);
  assert(same.offer(2, "0.9.0", kSha, true, "1.0.0", 0, 0) == OtaOffer::Unchanged);
  assert(!same.wantsDownload(1u << 30));
  return 0;
}
