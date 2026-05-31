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
    
    socket.on('join-channel', (chatID) => {
      socket.join(chatID);
      console.log(`User ${socket.id} joined channel ${chatID}`);
    });
    
    socket.on('leave-channel', (chatID) => {
      socket.leave(chatID);
      console.log(`User ${socket.id} left channel ${chatID}`);
    });
    
    socket.on('send-message', (chatID) => {
        console.log('bc to: ', chatID)
      // Broadcast to all users in the channel
      io.to(chatID).emit('receive-message', chatID)
    })
    
    socket.on('disconnect', () => {
      console.log('User disconnected:', socket.id);
    });
  }

  module.exports = socket