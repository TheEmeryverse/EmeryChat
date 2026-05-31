import io from 'socket.io-client';

// Create socket connection
const socket = io('http://localhost:3002', {
    withCredentials: true,
    transports: ['websocket', 'polling'],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 30000,
    randomizationFactor: 0.5,
    timeout: 60000,
})

export default socket;
