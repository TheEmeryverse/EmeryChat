require('../../env.js')
// const User = require('../../models/user')
// const Chat = require('../../models/chat.js')

//for security reasons, I can just use socket to broadcast to the frontend to make a new fetch call for data instead of actually broadcasting via socket

const socket = (io, socket) => {
    console.log('User connected:', socket.id);

    // register_online
    socket.on('register_online', (userData) => {
      // console.log('User disconnected:', socket.id);
      
    });

    // socket.on('join-channel', (userData) => {
    //   const {session, _id} = userData
      
    //   socket.join(channelId);
    //   console.log(`User ${socket.id} joined channel ${channelId}`);
    // });
    
    socket.on('join-channel', (channelId) => {
      socket.join(channelId);
      console.log(`User ${socket.id} joined channel ${channelId}`);
    });
    
    socket.on('leave-channel', (channelId) => {
      socket.leave(channelId);
      console.log(`User ${socket.id} left channel ${channelId}`);
    });
    
    socket.on('send-message', (data) => {
      const {channelId, chatID} = data
      // Broadcast to all users in the channel
      io.to(channelId).emit('receive-message', chatID)
    })
    
    socket.on('disconnect', () => {
      console.log('User disconnected:', socket.id);
    });
  }

  module.exports = socket