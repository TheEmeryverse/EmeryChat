import io from 'socket.io-client';

// Create socket connection
const socket = io('http://localhost:3002', {
    withCredentials: true,
    transports: ['websocket', 'polling'],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
    randomizationFactor: 0.5,
    timeout: 15000,
})

export default socket;
