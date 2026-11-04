#pragma once

// Reconcile rolling relay generations without confusing a connected socket
// with a usable Agent Endpoint. Current relays send authoritative `presence`;
// the established `peer.mac` field remains a firmware-compatibility projection
// of selected Link availability.
class EndpointAvailability {
 public:
  void clear() {
    peer_available_ = false;
    link_available_ = false;
    agent_available_ = false;
    presence_seen_ = false;
    agent_working_ = false;
    connection_in_progress_ = false;
  }

  bool applyPeer(bool available) {
    const bool was = usable();
    peer_available_ = available;
    return !was && usable();
  }

  bool applyPresence(bool link, bool agent, bool working = false, bool connecting = false) {
    const bool was = usable();
    presence_seen_ = true;
    link_available_ = link;
    agent_available_ = agent;
    agent_working_ = working;
    connection_in_progress_ = connecting;
    return !was && usable();
  }

  void applyDigest() {
    // A relay generation predating `presence` may establish availability by
    // forwarding a digest. Once presence exists, a digest cannot override an
    // explicit Agent-unavailable state.
    if (!presence_seen_) peer_available_ = true;
  }

  bool usable() const {
    if (presence_seen_) return link_available_ && agent_available_;
    return peer_available_;
  }

  // A live cloud can explain an unavailable endpoint aloud. The availability
  // state still drives the LED, but it must not suppress the request whose
  // fixed cloud response tells a screenless User what to repair.
  bool canRequest(bool cloud_connected) const { return cloud_connected; }

  bool working() const { return usable() && agent_working_; }
  bool connecting() const { return connection_in_progress_; }

  bool linkAvailable() const { return link_available_; }
  bool presenceSeen() const { return presence_seen_; }

 private:
  bool peer_available_ = false;
  bool link_available_ = false;
  bool agent_available_ = false;
  bool presence_seen_ = false;
  bool agent_working_ = false;
  bool connection_in_progress_ = false;
};
