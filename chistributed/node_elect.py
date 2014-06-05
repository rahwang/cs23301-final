from Queue import Queue
import random
import json
import sys
import signal
import time
import zmq
import math
from zmq.eventloop import ioloop, zmqstream
ioloop.install()

class Node:
  def __init__(self, node_name, pub_endpoint, router_endpoint, spammer, peer_names, prev_group, succ_group):
    sys.stdout = open('logging', 'a') 
    self.loop = ioloop.ZMQIOLoop.current()
    self.context = zmq.Context()
    # SUB socket for receiving messages from the broker
    self.sub_sock = self.context.socket(zmq.SUB)
    self.sub_sock.connect(pub_endpoint)
    # make sure we get messages meant for us!
    self.sub_sock.set(zmq.SUBSCRIBE, node_name)
    self.sub = zmqstream.ZMQStream(self.sub_sock, self.loop)
    self.sub.on_recv(self.handle)

    # REQ socket for sending messages to the broker
    self.req_sock = self.context.socket(zmq.REQ)
    self.req_sock.connect(router_endpoint)
    self.req = zmqstream.ZMQStream(self.req_sock, self.loop)
    self.req.on_recv(self.handle_broker_message)

    self.name = node_name
    '''
    if self.name in ['test1','test2','test3']:
        self.keyrange = ['foo']
    if self.name in ['test3','test4']:
        self.keyrange = ['bar']
    if self.name in ['test5','test6']:
        self.keyrange = ['baz']
    '''
    if self.name in ['testA','test2','test3','test4','test5']:
        self.keyrange = [i for i in range(1,6)]
    if self.name in ['test6','test7','test8','test9','test10']:
        self.keyrange = [i for i in range(6,11)]
    if self.name in ['test11','test12','test13','test14','test15']:
        self.keyrange = [i for i in range(11,16)]
    self.spammer = spammer
    self.peer_names = peer_names
    self.sent_id = 0
    self.succ_group = succ_group
    self.prev_group = prev_group
    self.dead = []
    self.outstanding_acks = []

    self.registered = False
    self.waiting = False
    self.seen_id = 0

    #self.group = group
    if self.peer_names[0] == self.name:
        self.leader = True
    else:
        self.leader = False
    if len(self.succ_group) > 1:
        self.prev_leader = self.prev_group[0]
        self.succ_leader = self.succ_group[0]
        self.forward_nodes = self.succ_group#[self.succ_leader]
        self.peer_leader = self.peer_names[0]
    else:
        self.forward_nodes = self.peer_names[:]
        self.forward_nodes.remove(self.name)

    # This the actual data storage. Takes the form {'key': (msg_id, value), ...}
    #self.store = {'foo': (0, None)}
    self.store = {}
    for k in self.keyrange:
        self.store[k] = (0, 'hello')
    for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT]:
      signal.signal(sig, self.shutdown)

    # Attributes for paxos get
    self.state = 'WAIT_PROPOSE'
    self.value = (0, None)
    self.consensus = False
    self.prepared = []
    self.promised = []
    self.nopromised = []
    self.accepted = []
    self.rejected = []
    self.group_tried = [False]*len(self.succ_group)
    
    # Attributes for Leader Election
    self.timeoutTime = 0.5
    self.leaderID = 1
    self.leaderPromiseID = 1
    self.leaderPromise = 1
    self.leaderUpdateTimeout = 0.5
    self.leaderUpdateTime = 0.0
    self.old_peer_leader = self.peer_leader
    self.old_leader_ref = None
    self.forwardMessage = None 
    
    self.electionVotes = 0
    self.electionVetos = 0
    self.vetoLeaderID = 0 
    

  def reqsendjson(self,msg):
    # Send msg
    #msg['source'] = self.name
    #if self.seen_id > int(msg['id']):
    try:
        if self.seen_id > int(msg['id']):
            return
    except KeyError:
         self.req.send_json({'type': 'log', 
                           'debug': {'event': 'error', 
                                     'node': self.name, 
                                     'msg': msg}})
    self.req.send_json(msg)
    #if self.name == 'test3' and msg['type'] == 'get':
        #print msg

    # The following message types need acks
    if msg['type'] in ['get', 'set', 'getReply', 'setReply', 'nodeset']:
      self.loop.add_timeout(time.time() + 0.5, lambda: self.collectAcks(msg))
      for i in msg['destination']:
        if i != self.name:
            self.outstanding_acks.append((i, msg['id']))
            self.outstanding_acks[:] = [a for a in self.outstanding_acks if int(a[1]) >= int(msg['id'])]
      
      
  def start(self):
    '''
    Simple manual poller, dispatching received messages and sending those in
    the message queue whenever possible.
    '''
    self.loop.start()

  def handle_broker_message(self, msg_frames):
    '''
    Nothing important to do here yet.
    '''
    pass
  def sendError(self, msg):
    if not self.waiting:
        return
    #print "send error:",msg
    if self.sent_id >= msg['id']:
        return
    self.reqsendjson({'type': msg['type'] + 'Response', 'key': msg['key'], 'id': msg['id'], 'error': 'Key not accessible'})    
    self.waiting = False

  def majority(self, nodes):
    #print len(nodes), math.ceil((len(self.peer_names) - len(self.dead)) / 2)
    if len(nodes) >= math.ceil((len(self.peer_names) - len(self.dead)) / 2):
      return True
    return False

  # If node is the origin, wait for correct get/set info to be forwarded.
  # In case of timeout, send an error response for get
  def collectReply(self, msg):
    if msg['origin'] == self.name:
      self.loop.add_timeout(time.time() + 10, lambda: self.sendError(msg))
      #if not self.receive():
      #  self.reqsendjson({'type': msg['type'] + 'Response', 'key': msg['key'], 'id': msg['id'], 'error': 'Key not accessible'})    

  def collectAcks(self, msg):
    #if self.name == 'test2':
      #print 'outstanding acks', self.outstanding_acks, msg['type']
    try:
        if self.seen_id > int(msg['id']):
            return
    except KeyError:
         self.req.send_json({'type': 'log', 
                           'debug': {'event': 'error', 
                                     'node': self.name, 
                                     'msg': msg}})
        
    if msg['type'] == 'nodeset':
      for n in self.peer_names:
        if ((n,msg['id']) in self.outstanding_acks) and (n not in self.dead):
          self.dead.append(n)
      #print 'nodeset replying',self.name
      msg['type'] = 'set'
      self.reply(msg)
      return
    if ((msg['destination'][0],msg['id']) not in self.outstanding_acks):
        return 
    elif msg['type'] in ['get', 'set']:
      #print "OUTSTANDING ACK", msg['destination']
      if msg['destination'] == [self.peer_leader]:
        # Start leader election
        print "current message: ", msg
        print "outstanding acks: ", self.outstanding_acks
        self.outstanding_acks[:] = [i for i in self.outstanding_acks if i != (msg['source'],msg['id'])]
        if self.old_leader_ref == None:
          self.old_leader_ref = msg['destination'][0]
          self.forwardMessage = msg.copy()
          self.leaderElection(msg.copy())
        pass
      elif msg['destination'][0] in self.peer_names:
        pass
      else:
        i = (self.succ_group.index(msg['destination'][0]) + 1) % len(self.succ_group)
        self.outstanding_acks[:] = [x for x in self.outstanding_acks if x != (msg['destination'][0],msg['id']) and int(x[1]) >= self.seen_id]
        self.group_tried[self.succ_group.index(msg['destination'][0])] = True
        #if self.succ_group[i]:
        #    self.sendError(msg)
        msg['destination'] = [self.succ_group[i]]
        msg['source'] = self.name
        if msg['type'] == 'get' and self.name == 'test33':
         self.req.send_json({'type': 'log', 
                           'debug': {'event': 'getting', 
                                     'node': self.name, 
                                     'destination': msg['destination'],
                                     'acks': self.outstanding_acks}})
        if self.waiting:
          self.reqsendjson(msg)
        #print 'next node', msg
        # Try sending to the next node
        pass
    elif msg['type'] in ['getReply', 'setReply']:
      if msg['destination'] == msg['origin']:
        # Fail
        pass
      else:
        # Try sending to the next node
        pass

  def collectPrepare(self, msg):
    # TODO check failed
    for n in self.peer_names:
      if (n not in self.dead) and (n not in self.prepared):
        self.dead.append(n)
      #self.prepared = []

  def collectPromise(self, msg):
    # TODO check failed
    k = msg['key']
    self.promised = list(set(self.promised))
    self.nopromised = list(set(self.nopromised))
    #print 'promises', self.name, self.promised
    #if self.majority(self.promised):# and self.state == 'WAIT_PROMISE':
    if len(self.promised) > len(self.nopromised):
      self.value = self.store[k]#299msg['value']
      #print 'promise majority'
      self.state = "WAIT_ACCEPTED"
      self.reqsendjson({'type': 'accept', 
                          'key': k,
                          'value': self.store[k],
                          'source': self.name,
                          'destination': self.peer_names,
                          'id': msg['id']})
      self.loop.add_timeout(time.time() + .5, lambda: self.collectAccepted(msg))
      
  def collectAccepted(self, msg):
    # TODO check failed
    k = msg['key']
    self.accepted = list(set(self.accepted))
    #if self.majority(self.accepted):
    if len(self.accepted) > len(self.rejected):
      #print 'majority'
      self.state = "CONSENSUS"
      self.reqsendjson({'type': 'consensus', 
                          'key': k,
                          'value': self.store[k],
                          'source': self.name,
                          'destination': self.peer_names,
                          'id': msg['id']})
    #elif self.majority(self.rejected):
    else:
      self.state = "WAIT_PROMISE"
      self.reqsendjson({'type': 'prepare', 
                          'key': k,
                          'value': self.store[k],
                          'source': self.name,
                          'destination': self.peer_names,
                          'id': msg['id']})
      self.loop.add_timeout(time.time() + .5, lambda: self.collectPromise(msg)) 

  def handle(self, msg_frames):
    assert len(msg_frames) == 3
    #if msg_frames[0] != self.name:
        #print msg_frames[0], self.name
    assert msg_frames[0] == self.name
    # Second field is the empty delimiter
    msg = json.loads(msg_frames[2])
    self.handle_message(msg)
    
  def handle_message(self, msg):
    #print "message received: ", msg
    #if 'origin' in msg.keys() and msg['origin'] == 'test3':
    #    print msg
    if 'id' not in msg.keys():
        msg['id'] = 0
    if self.seen_id > int(msg['id']):
        return
    self.seen_id = max(self.seen_id, int(msg['id']))
    #if msg['type'] != 'hello':
      #print msg['type'], self.name, msg['id'],
      #if 'source' in msg.keys():
        #print msg['source']

    if msg['type'] in ['propose', 'promise', 'nopromise', 'accepted', 'rejected', 'accept', 'prepare', 'consensus']:
      self.handle_paxos(msg)
      return
    elif msg['type'] in ['leaderElec', 'leaderVeto', 'leaderVote','leaderAccept']:
      self.handle_leader_elect(msg)
      return
    elif msg['type'] == 'ack':
      # TODO SOME HANDLING
      #self.reqsendjson({'type': 'log', 
      #                  'debug': {'event': 'ack_receive', 
      #                            'node': self.name, 
      #                            'destination': msg['destination'],
      #                            'acks': self.outstanding_acks,
      #                            'check': (msg['source'],msg['id'])}})
      if (msg['source'],msg['id']) in self.outstanding_acks:
        orig = self.outstanding_acks[:]
        self.outstanding_acks[:] = [i for i in self.outstanding_acks if i != (msg['source'],msg['id'])]
        #self.reqsendjson({'type': 'log', 
        #                  'debug': {'event': 'ack_remove', 
        #                            'node': self.name, 
        #                            'destination': msg['destination'],
        #                            'acks': orig,
        #                            'acks_after': self.outstanding_acks,
        #                            'check': (msg['source'], msg['id']),
        #                            'id': self.sent_id}})
      return
    elif msg['type'] == 'hello':
      # should be the very first message we see
      if not self.registered:
        self.reqsendjson({'type': 'hello', 'id': 0, 'source': self.name})
        self.registered = True
      return
   
    # Messages which require acks
    
    msg['key'] = int(msg['key'])
    k = msg['key']
    if 'value' in msg.keys():
      v = msg['value'] 
    
    if 'source' not in msg.keys():
      msg['source'] = self.name
    if 'origin' not in msg.keys():
      msg['origin'] = self.name
      self.waiting = True
    #self.sendack(msg)  
    
    if msg['type'] == 'get':
      # TODO: handle errors, esp. KeyError
      if k in self.keyrange:
        v = self.store[k][1]
      else:
        v = (0, '')
      
      #self.reqsendjson({'type': 'log', 
                          #'debug': {'event': 'getting', 
                          #          'node': self.name, 
                          #          'key': k, 
                          #          'value': v}})
      if 'source' not in msg.keys():
        msg['source'] = self.name
      if 'origin' not in msg.keys():
        msg['origin'] = self.name
        self.waiting = True
      if not self.forward(msg):
        self.consistentGet(k, msg)
      self.collectReply(msg)
      #print self.name, 'collectReplied'
    elif msg['type'] == 'set':
      #self.reqsendjson({'type': 'log', 
                        #  'debug': {'event': 'setting', 
                        #            'node': self.name, 
                        #            'key': k, 
                        #            'value': v}})
      if 'source' not in msg.keys():
        msg['source'] = self.name
      if 'origin' not in msg.keys():
        msg['origin'] = self.name
        self.waiting = True
      if not self.forward(msg):
        self.consistentSet(k, v, msg)
    elif msg['type'] == 'nodeset':
      if k in self.keyrange:
        self.store[k] = (msg['id'], v)
        #print 'NODESET', self.store[k], self.name
    elif msg['type'] == 'getReply':
      self.reply(msg)
    elif msg['type'] == 'setReply':
      self.reply(msg)
    else:
      self.reqsendjson({'type': 'log', 
                          'debug': {'event': 'unknown', 
                                    'prev_type': msg['type'],
                                    'node': self.name}})
    self.sendack(msg)

  def sendack(self, msg2):
    msg = msg2.copy()
    #print "sending ack: ", msg2
    if 'source' in msg.keys() and msg['source'] != self.name:
      msg['prevtype'] = msg['type']
      msg['type'] = 'ack'
      msg['destination'] = [msg['source']]
      msg['source'] = self.name
      self.reqsendjson(msg)

  def handle_paxos(self, msg):
    k = msg['key']
    if 'value' in msg.keys():
      v = msg['value'] 
    
    if msg['type'] == 'propose':
      #print 'received propose', self.name
      if ((self.state == 'CONSENSUS') and (msg['id'] != self.store[k][0])) or (self.state == 'WAIT_PROPOSE'):
        self.state = 'WAIT_PROMISE'
        self.value = self.store[k]
        self.reqsendjson({'type': 'prepare', 
                            'key': k,
                            'value': self.store[k],
                            'source': self.name,
                            'destination': self.peer_names,
                            'id': msg['id']})
        #print 'sending prepare', self.name, 'to', self.peer_names
        self.loop.add_timeout(time.time() + 1.5, lambda: self.collectPromise(msg))
    elif msg['type'] == 'promise':
      if self.state == 'WAIT_PROMISE':
        self.promised.append(msg['source'])
    elif msg['type'] == 'nopromise':
      if self.state == 'WAIT_PROMISE':
        self.nopromised.append(msg['source'])
    elif msg['type'] == 'accepted':
      if self.state == 'WAIT_ACCEPTED':
        self.accepted.append(msg['source'])
    elif msg['type'] == 'rejected':
      if self.state == 'WAIT_ACCEPTED':
        self.rejected.append(msg['source'])
    elif msg['type'] == 'prepare':
      #print 'received prepare', self.name, 'from', msg['source'], msg['value'], self.value
      if msg['value'][0] >= self.value[0]:
        self.store[msg['key']] = msg['value']
        self.promised = []
        self.accepted = []
        self.rejected = []
        self.prepared.append(msg['source']) 
        self.reqsendjson({'type': 'promise', 
                            'key': k,
                            'value': self.store[k],
                            'source': self.name,
                            'destination': [msg['source']],
                            'id': msg['id']})
      else:
        self.reqsendjson({'type': 'nopromise',
                            'source': self.name,
                            'destination': [msg['source']],
                            'id': msg['id'],
                            'key': k})
    elif msg['type'] == 'accept':
      #print 'accept message', self.value, msg['value'], self.name
      if self.value == msg['value']:
        self.reqsendjson({'type': 'accepted', 
                            'key': k,
                            'value': self.store[k],
                            'source': self.name,
                            'destination': [msg['source']],
                            'id': msg['id']})
      elif self.value[0] < msg['value'][0]:
        self.reqsendjson({'type': 'rejected', 
                            'key': k,
                            'value': self.store[k],
                            'source': self.name,
                            'destination': msg['source'],
                            'id': msg['id']})
    elif msg['type'] == 'consensus':
      self.consensus = True
      self.value = msg['value']
      self.store[msg['key']] = self.value
      self.state = 'CONSENSUS'
    else:
      self.reqsendjson({'type': 'log', 
                          'debug': {'event': 'unknown', 
                                    'node': self.name}})

  def shutdown(self, sig, frame):
    self.loop.stop()
    self.sub_sock.close()
    self.req_sock.close()
    sys.exit(0)

  # Forwards msg to correct nodes. Returns True is msg forwarded, False if no forwarding needed
  def forward(self, msg):
    #print 'forwarding', msg['type'], 'leader', self.leader, 'key', msg['key'], self.keyrange
    #if msg['type'] == 'get':
        #print 'forwarding', self.leader, self.name, self.forward_nodes
    if msg['key'] not in self.keyrange:
      self.group_tried = [False]*len(self.succ_group)
      #print msg['key'], self.keyrange
      msg['destination'] = self.forward_nodes #[self.succ_leader, self.prev_leader] #self.peer_names
      msg['source'] = self.name
      #print msg
      self.reqsendjson(msg) 
    elif not self.leader:
      #print 'sending to recipient', msg['type']
      msg['destination'] = [self.peer_leader]
      msg['source'] = self.name
      #print msg
      self.reqsendjson(msg) 
    else:
      return False
    return True

  def reply(self, msg):
    #print 'origin:', msg['type'], msg['origin'], self.name
    if msg['origin'] != self.name:
      if msg['origin'] in self.peer_names:
        msg[u'destination'] = [msg['origin']]
      else:
        # Else change to next_names
        msg['destination'] = self.forward_nodes
      msg['type'] = msg['type'][:3] + 'Reply'
      msg['source'] = self.name
      #if msg['origin'] == 'test3':
      #   print msg
      self.reqsendjson(msg)
      #print 'sent'
    else:
      #print 'id', msg['id'], self.sent_id
      if self.sent_id >= int(msg['id']):
        return
      msg['type'] = msg['type'][:3] + 'Response'
      self.sent_id = int(msg['id'])
      #print msg
      self.reqsendjson(msg)
      self.waiting = False
      #if msg['type'] == 'getResponse':
        #print self.name, 'accepted:', self.accepted, 'rejected:', self.rejected, 'promised:', self.promised, self.value
      
  def consistentSet(self, k, v, msg):
    #print msg
    new_msg = {'type': 'nodeset', 
               'key' : k, 
               'value' : v, 
               'source': self.name, 
               'destination': self.peer_names, 
               'origin': msg['origin'],
               'id': msg['id']}
    self.reqsendjson(new_msg)
    self.loop.add_timeout(time.time() + .5, lambda: self.collectAcks(new_msg))

  def consistentGet(self, k, msg):
    #START PAXOS
    self.reqsendjson({'type': 'propose', 
                        'key' : k, 
                        'value' : None, 
                        'source': self.name, 
                        'destination': self.peer_names, 
                        'id': msg['id']})    
    #self.loop.add_timeout(time.time() + .5, lambda: self.collectPrepare(msg))

    self.loop.add_timeout(time.time() + 4, lambda: self.checkConsensus(msg))

    #v = self.store[k][1]
    #msg['value'] = v
    #self.reply(msg)

  def checkConsensus(self, msg):
    if self.consensus:
      #print 'self.value', self.value
      self.store[msg['key']] = self.value
      msg['value'] = self.value[1]
      msg['type'] = 'get'
      #print 'consensus message', msg
      self.reply(msg)
    else:
      
      #new_msg = 
      self.reply({'type': msg['type'] + 'Response', 'key': msg['key'], 'id': msg['id'], 'value': 'No consensus reached', 'origin': msg['origin']})
      #new_msg['origin'] = msg['origin']
      #self.reply(new_msg)


  def handle_leader_elect(self, msg):
    if msg['type'] == 'leaderElec' and msg['origin'] != self.name:
      if self.leaderPromiseID >= msg['leadid'] or (self.leaderPromiseID == msg['leadid'] and self.leaderPromise != msg['origin']):
        self.req.send_json({'type': 'leaderVeto', 'leadid': self.leaderPromiseID, 'id': msg['id'], 'origin': msg['origin'], 'source': self.name, 'destination': [msg['origin']] })
        self.leaderUpdateTime = self.loop.time()
        print "leaderElec - ", self.name, " received leaderElec from ", msg['origin']," proposalID: ",  msg['leadid'], "current leaderID and promise: ", self.leaderID,self.leaderPromiseID, "- so Veto"
      elif (self.leaderPromiseID < msg['leadid']):
		    self.leaderPromiseID = msg['leadid']
		    self.leaderPromise = msg['origin']
		    self.leaderUpdateTime = self.loop.time()
		    self.req.send_json({'type': 'leaderVote', 'leadid': self.leaderPromiseID, 'id': msg['id'], 'origin': msg['origin'], 'source': self.name, 'destination': [msg['origin']] })
		    print "leaderElec - ", self.name, " received leaderElec from ", msg['origin']," proposalID: ",  msg['leadid'], "current leaderID and promise: ", self.leaderID,self.leaderPromiseID, "- so Vote"
    elif msg['type'] == 'leaderVeto':
      if (msg['leadid'] == self.leaderID):
        self.electionVetos = self.electionVetos + 1
        if self.vetoLeaderID < msg['leadid']:
          self.vetoLeaderID = msg['leadid']
        print "leaderVeto - ",  self.name, " received Veto message from ", msg['origin']
    elif msg['type'] == 'leaderVote':
      if msg['leadid'] == self.leaderID:
        self.electionVotes = self.electionVotes + 1
        print "leaderVote - ",  self.name, " received Vote message from ", msg['source']
    elif msg['type'] == 'leaderAccept':
			if ( msg['leadid'] > self.leaderID ):
			#if ( msg['leadid'] == self.leaderPromiseID ) and ( self.leaderPromise == msg['source'] ):
				self.leaderID = self.leaderPromiseID
				self.old_peer_leader = self.peer_leader
				self.peer_leader = msg['source']
				self.leader = False
				self.old_leader_ref = None
				self.leaderUpdateTime = self.loop.time()
				#for i, (k, v) in iter(self.outstanding_acks):
				#  if k == msg['source']:
				#    self.outstanding_acks.remove(i)
				self.outstanding_acks[:] = [ (i,j) for (i,j) in self.outstanding_acks if i != msg['source']]

				if (self.forwardMessage != None):
				  self.forwardMessage['destination'] = [self.peer_leader]
				  self.handle_message(self.forwardMessage)
				  self.forwardMessage = None
				print "NEW LEADER IS", self.peer_leader, "SAYS",  self.name  
  
  def leaderElection(self,msg):
    if (self.peer_leader == self.old_leader_ref):
      if ( self.loop.time() - self.leaderUpdateTime ) > self.leaderUpdateTimeout:
        print "New Leader election for: ", self.name
        self.leaderID = self.leaderID + 1
        self.electionVotes = 0
        self.electionVetos = 0
        self.vetoLeaderID = 0
        
        #msg['id'] = msg['id'] + 1
        #print "Sending leader election from ", self.name,
        self.req.send_json({'type': 'leaderElec', 'leadid': self.leaderID, 'id':msg['id'] , 'origin': self.name, 'source': self.name, 'destination': self.peer_names})
        self.loop.add_timeout(self.loop.time() + self.timeoutTime, lambda: self.tallyElection(msg) ) 
      else:
        self.loop.add_timeout(self.loop.time() + self.timeoutTime, lambda: self.leaderElection(msg) ) 

  def tallyElection(self,msg):
    if (self.peer_leader == self.old_leader_ref):
      if self.electionVotes > self.electionVetos:
        self.peer_leader = self.name
        self.old_leader_ref = None
        self.leader = True
        #msg['id'] = msg['id'] + 10
        self.req.send_json({'type': 'leaderAccept', 'leadid': self.leaderID, 'id':msg['id'] , 'origin': self.name, 'source': self.name, 'destination': self.peer_names})
        if (self.forwardMessage != None):
          self.forwardMessage['destination'] = [self.peer_leader]
          self.handle_message(self.forwardMessage)
          self.forwardMessage = None
      else:
        self.leaderID = self.vetoLeaderID
        print "Election Results for ", self.name, "- Votes: ", self.electionVotes,  "- Vetos: ", self.electionVetos,
        self.leaderElection(msg)


if __name__ == '__main__':
  import argparse
  parser = argparse.ArgumentParser()
  parser.add_argument('--pub-endpoint',
      dest='pub_endpoint', type=str,
      default='tcp://127.0.0.1:23310')
  parser.add_argument('--router-endpoint',
      dest='router_endpoint', type=str,
      default='tcp://127.0.0.1:23311')
  parser.add_argument('--node-name',
      dest='node_name', type=str,
      default='test_node')
  parser.add_argument('--spammer',
      dest='spammer', action='store_true')
  parser.set_defaults(spammer=False)
  parser.add_argument('--peer-names',
      dest='peer_names', type=str,
      default='')
  parser.add_argument('--succ-group',
      dest='succ_group', type=str,
      default='')
  parser.add_argument('--prev-group',
      dest='prev_group', type=str,
      default='')
 # parser.add_argument('--group',
 #     dest='group', type=str,
 #     default='')
  args = parser.parse_args()
  args.peer_names = args.peer_names.split(',')
  args.prev_group = args.prev_group.split(',')
  args.succ_group = args.succ_group.split(',')
  #args.group = int(args.group)
  Node(args.node_name, args.pub_endpoint, args.router_endpoint, args.spammer, args.peer_names, args.prev_group, args.succ_group).start()

